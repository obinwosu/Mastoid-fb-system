"""Canonical recorder schemas, asynchronous camera sampling and native outputs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Check optional dependencies before importing the source modules that use them.
h5py = pytest.importorskip("h5py")
np = pytest.importorskip("numpy")
from scipy.spatial.transform import Rotation  # noqa: E402

from evidence_system.frame_pipeline.core_frames import extract_core_frames  # noqa: E402
from evidence_system.hdf5_clock import (  # noqa: E402
    generate_hdf5_alignment,
    read_hdf5_frame_inventory,
)
from evidence_system.hdf5_reader import (  # noqa: E402
    CAMERA_POSE,
    CAMERA_TIME,
    HDF5SchemaError,
    RecordingReader,
)
from evidence_system.registry import get_run, load_registry  # noqa: E402
from evidence_system.review_video import build_review_video  # noqa: E402
from evidence_system.source_config import ResolvedRunSources  # noqa: E402


def _camera(times):
    t = np.asarray(times) - 100.0
    p = np.zeros((len(t), 7))
    p[:, 0] = t * 2.0
    p[:, 3:] = Rotation.from_euler("z", t[:, None] * 180.0, degrees=True).as_quat()
    return p


def _v3(path, vision_times=(), camera_times=(), removal_times=()):
    with h5py.File(path, "w") as h:
        h.create_dataset("metadata_v3/voxel_volume", data=0.1)
        h.create_dataset("metadata_v3/camera_intrinsic", data=np.eye(3))
        h.create_dataset("metadata_v3/camera_extrinsic", data=np.eye(4))
        if len(vision_times):
            prefix = "vision_data/continuous_data/"
            h.create_dataset(prefix + "time", data=vision_times)
            images = np.empty((len(vision_times), 32, 32, 3), np.uint8)
            images[:] = [220, 40, 10]
            h.create_dataset(prefix + "l_img", data=images)
            h.create_dataset(prefix + "depth", data=np.ones((len(vision_times), 32, 32)))
            for name in ("pose_mastoidectomy_drill", "pose_mastoidectomy_volume"):
                h.create_dataset(
                    prefix + name,
                    data=np.tile([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0], (len(vision_times), 1)),
                )
        if len(camera_times):
            h.create_dataset(CAMERA_TIME, data=camera_times)
            h.create_dataset(CAMERA_POSE, data=_camera(camera_times))
        if len(removal_times):
            prefix = "physics_data/intermittent_data/voxels_removed/"
            h.create_dataset(prefix + "voxel_time_stamp", data=removal_times)
            h.create_dataset(
                prefix + "voxel_removed", data=[[i, i + 1, 2, 3] for i in range(len(removal_times))]
            )


def _record():
    return get_run("DEMO_CASE_01", load_registry(ROOT / "config/examples/runs.example.csv"))


def _sources(raw, tmp_path, alignment=None):
    return ResolvedRunSources(
        raw_run_dir=raw,
        edt_dir=tmp_path,
        phase_annotation_file=None,
        golden_reference_path=tmp_path / "unused.json",
        phase_model_bundle=tmp_path,
        frame_alignment_file=Path(alignment["alignment_path"]) if alignment else None,
        phase_duration_s=alignment["duration_s"] if alignment else None,
    )


def test_v3_camera_uses_recording_global_time_and_quaternion_slerp(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    a, z = raw / "a.h5", raw / "z.hdf5"
    _v3(a, [100.025, 100.075], [100.1, 100.15, 100.2], [100.02])
    _v3(z, [100.125, 100.175], [100.0, 100.05], [100.12])
    _v3(raw / "m_physics_only.h5", removal_times=[100.17])
    reader = RecordingReader(raw)
    assert reader.schema == "v3"
    assert reader.chunk_role(raw / "m_physics_only.h5") == "physics_only"
    for path, times in ((a, [100.025, 100.075]), (z, [100.125, 100.175])):
        with reader.open_chunk(path) as view:
            assert isinstance(view["data/l_img"], h5py.Dataset)
            assert isinstance(view["data/depth"], h5py.Dataset)
            assert view["data/l_img"].name == "/vision_data/continuous_data/l_img"
            actual = view["data/pose_main_camera"]
            np.testing.assert_allclose(actual[:, :3], _camera(times)[:, :3], atol=1e-14)
            np.testing.assert_allclose(
                Rotation.from_quat(actual[:, 3:]).as_matrix(),
                Rotation.from_quat(_camera(times)[:, 3:]).as_matrix(),
                atol=1e-14,
            )
            # A timestamp index of zero in each file refers to that file's first event.
            assert int(view["voxels_removed/voxel_removed"][0, 0]) == 0
    report = reader.manifest()["camera_alignment"]
    assert report["status"] == "complete" and report["interpolated_frame_count"] == 4
    assert report["physics_sample_count"] == 5 and not report["missing_frames"]
    clock = generate_hdf5_alignment(raw, tmp_path / "clock")
    rows, inventory = read_hdf5_frame_inventory(raw)
    assert len(rows) == 4 and len(inventory) == 3
    core = extract_core_frames(_record(), _sources(raw, tmp_path, clock), project_root=ROOT)
    assert len(core.frames) == 4 and len(core.removed_voxels) == 3
    assert [r["frame_idx_global"] for r in core.removed_voxels] == [0, 2, 3]
    assert core.manifest["physics_only_files"] == ["m_physics_only.h5"]
    assert all(r["camera_pose_valid"] for r in core.frames)
    assert "vision_data/continuous_data/l_img" in core.frames[0]["rgb_frame_ref"]


def test_camera_gaps_and_outside_coverage_remain_missing_without_dropping_vision(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    times = [99.9, 100.0, 100.5, 101.0, 101.1]
    _v3(raw / "record.hdf5", times, [100.0, 101.0])
    reader = RecordingReader(raw)
    with reader.open_chunk(reader.paths[0]) as view:
        pose = view["data/pose_main_camera"]
    assert pose.shape == (5, 7)
    assert np.isnan(pose[[0, 2, 4]]).all()
    np.testing.assert_allclose(pose[[1, 3]], _camera([100.0, 101.0]))
    report = reader.manifest()["camera_alignment"]
    assert report["missing_frame_count"] == 3 and report["outside_coverage_count"] == 2
    assert report["long_gap_count"] == 1 and report["max_allowed_bracket_gap_s"] == 0.1
    assert [r["source_frame_idx"] for r in report["missing_frames"]] == [0, 2, 4]
    core = extract_core_frames(_record(), _sources(raw, tmp_path), project_root=ROOT)
    assert len(core.frames) == 5
    assert [r["camera_pose_valid"] for r in core.frames] == [False, True, False, True, False]
    assert all(core.frames[i]["camera_qw"] is None for i in [0, 2, 4])
    json.dumps(core.frames, allow_nan=False)


def test_quaternion_sign_flip_follows_short_arc(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    p = raw / "record.hdf5"
    _v3(p, [100.025], [100.0, 100.05])
    with h5py.File(p, "r+") as h:
        h[CAMERA_POSE][1, 3:] *= -1
    with RecordingReader(raw).open_chunk(p) as view:
        pose = view["data/pose_main_camera"]
    np.testing.assert_allclose(
        Rotation.from_quat(pose[:, 3:]).as_matrix(),
        Rotation.from_quat(_camera([100.025])[:, 3:]).as_matrix(),
        atol=1e-14,
    )


@pytest.mark.parametrize(
    "variant",
    [
        "unknown_root",
        "unknown_vision",
        "unknown_physics",
        "mixed_chunks",
        "mixed_roots",
        "duplicate_camera",
        "partial_removal",
    ],
)
def test_unknown_mixed_and_malformed_data_cannot_be_metadata_only(tmp_path, variant):
    raw = tmp_path / "raw"
    raw.mkdir()
    p = raw / "a.hdf5"
    if variant == "unknown_root":
        with h5py.File(p, "w") as h:
            h.create_dataset("new_frames/timestamps", data=[100.0])
    else:
        _v3(p, [100.025], [100.0, 100.05])
        if variant == "mixed_chunks":
            with h5py.File(raw / "b.hdf5", "w") as h:
                h.create_group("metadata")
        elif variant == "duplicate_camera":
            _v3(raw / "b.hdf5", [100.075], [100.05, 100.1])
        else:
            with h5py.File(p, "r+") as h:
                if variant == "unknown_vision":
                    h.create_dataset("vision_data/renamed_image_stream", data=[1])
                if variant == "unknown_physics":
                    h.create_dataset("physics_data/new_removal_stream", data=[1])
                if variant == "mixed_roots":
                    h.create_group("metadata")
                if variant == "partial_removal":
                    h.create_dataset(
                        "physics_data/intermittent_data/voxels_removed/voxel_removed",
                        data=[[0, 1, 2, 3]],
                    )
    with pytest.raises(HDF5SchemaError):
        RecordingReader(raw)


def test_legacy_camera_and_images_are_exact_unmodified_datasets(tmp_path):
    p = tmp_path / "record.hdf5"
    poses = _camera([100.0, 100.05])
    with h5py.File(p, "w") as h:
        h.create_dataset("data/time", data=[100.0, 100.05])
        h.create_dataset("data/l_img", data=np.arange(24, dtype=np.uint8).reshape(2, 2, 2, 3))
        h.create_dataset("data/pose_main_camera", data=poses)
    with h5py.File(tmp_path / "terminal.hdf5", "w") as h:
        h.create_group("metadata")
    reader = RecordingReader(tmp_path)
    assert (
        reader.schema == "legacy"
        and reader.chunk_role(tmp_path / "terminal.hdf5") == "metadata_only"
    )
    with reader.open_chunk(p) as view:
        assert isinstance(view["data/pose_main_camera"], h5py.Dataset)
        np.testing.assert_array_equal(view["data/pose_main_camera"][()], poses)
        np.testing.assert_array_equal(
            view["data/l_img"][()], np.arange(24, dtype=np.uint8).reshape(2, 2, 2, 3)
        )


def test_v3_clock_core_and_actual_video_use_the_same_native_rows(tmp_path):
    pytest.importorskip("cv2")
    raw = tmp_path / "raw"
    raw.mkdir()
    camera_times = np.linspace(100.0, 101.3, 27)
    _v3(raw / "record.hdf5", [100.0, 100.23, 101.23], camera_times)
    clock = generate_hdf5_alignment(raw, tmp_path / "clock")
    sources = _sources(raw, tmp_path, clock)
    core = extract_core_frames(_record(), sources, project_root=ROOT)
    video = build_review_video(
        _record(),
        sources,
        tmp_path / "video",
        fps=10.0,
        frame_times_video=[r["t_video_s"] for r in core.frames],
        clock_manifest=core.manifest["clock_alignment"],
    )
    assert video["source_image_dataset"] == "vision_data/continuous_data/l_img"
    assert video["source_frame_count"] == 3 and video["output_frame_count"] == 14
    assert video["validation"]["decode_ok"] and video["validation"]["color_order_check_passed"]
