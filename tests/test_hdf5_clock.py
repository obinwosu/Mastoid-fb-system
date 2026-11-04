"""Native-source clock binding and real HDF5/core/VP8 integration tests."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Check optional dependencies before importing the source modules that use them.
h5py = pytest.importorskip("h5py")
np = pytest.importorskip("numpy")

from evidence_system import phase_detection  # noqa: E402
from evidence_system.frame_pipeline.core_frames import (  # noqa: E402
    extract_core_frames,
    resolve_frame_clock,
)
from evidence_system.hdf5_clock import (  # noqa: E402
    SOURCE,
    generate_hdf5_alignment,
    list_hdf5_paths,
    read_hdf5_frame_inventory,
    sha256_file,
)
from evidence_system.phase_features import FEATURES, REMOVAL, SOURCE_COLUMNS  # noqa: E402
from evidence_system.registry import get_run, load_registry  # noqa: E402
from evidence_system.review_video import build_review_video  # noqa: E402
from evidence_system.source_config import ResolvedRunSources  # noqa: E402


def _chunk(path, times, *, removals=()):
    times = np.asarray(times, dtype=float)
    images = np.zeros((len(times), 32, 32, 3), np.uint8)
    images[:] = [220, 40, 10]
    pose = np.tile([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0], (len(times), 1))
    with h5py.File(path, "w") as handle:
        handle.create_dataset("data/time", data=times)
        handle.create_dataset("data/l_img", data=images, compression="gzip")
        for name in ("mastoidectomy_drill", "mastoidectomy_volume", "main_camera"):
            handle.create_dataset("data/pose_" + name, data=pose)
        handle.create_dataset("metadata/voxel_volume", data=0.1)
        if removals:
            handle.create_dataset("voxels_removed/voxel_time_stamp", data=removals)
            handle.create_dataset(
                "voxels_removed/voxel_removed", data=[[i, 1, 2, 3] for i in range(len(removals))]
            )


def _fixture(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    # Names deliberately disagree with temporal order; both suffix families count.
    _chunk(raw / "z_early.H5", [100.0, 100.17], removals=[100.1])
    _chunk(raw / "a_late.hdf5", [100.62, 101.23])
    with h5py.File(raw / "terminal.HDF5", "w") as handle:
        handle.create_group("metadata")
    record = get_run("DEMO_CASE_01", load_registry(ROOT / "config/examples/runs.example.csv"))
    receipt = generate_hdf5_alignment(raw, tmp_path / "clock", fps=10.0)
    sources = ResolvedRunSources(
        raw_run_dir=raw,
        edt_dir=tmp_path,
        phase_annotation_file=None,
        golden_reference_path=tmp_path / "absent.json",
        phase_model_bundle=tmp_path,
        frame_alignment_file=Path(receipt["alignment_path"]),
        phase_duration_s=receipt["duration_s"],
    )
    return raw, record, sources, receipt


def test_real_native_source_core_video_chain_and_source_immutability(tmp_path):
    pytest.importorskip("cv2")
    raw, record, sources, receipt = _fixture(tmp_path)
    before = {p.name: sha256_file(p) for p in list_hdf5_paths(raw)}
    assert len(before) == 3
    with Path(receipt["alignment_path"]).open(newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == [
            "frame_idx_global",
            "source_hdf5",
            "source_frame_idx",
            "t_abs",
            "video_time_s",
        ]
        rows = list(reader)
    assert [row["source_hdf5"] for row in rows] == ["z_early.H5"] * 2 + ["a_late.hdf5"] * 2
    assert [int(row["source_frame_idx"]) for row in rows] == [0, 1, 0, 1]
    expected = np.array([100.0, 100.17, 100.62, 101.23]) - 100.0
    np.testing.assert_array_equal([float(row["video_time_s"]) for row in rows], expected)
    receipt_json = json.loads(Path(receipt["manifest_path"]).read_text())
    assert receipt_json["training_reference_clock"] is False
    assert receipt_json["historical_video_or_csv_used"] is False
    assert receipt_json["source_inventory"][1]["metadata_only"] is True
    assert receipt_json["all_source_rgb_frames_readable"]
    core = extract_core_frames(record, sources, project_root=ROOT)
    clock = core.manifest["clock_alignment"]
    assert clock["source"] == SOURCE and clock["native_correspondence_verified"]
    assert clock["phase_columns_consumed"] is False
    assert clock["world_frame_index_present"] is False
    assert clock["duration_s"] == receipt["duration_s"] == float(expected[-1])
    assert [row["t_abs"] for row in core.frames] == [100.0, 100.17, 100.62, 101.23]
    np.testing.assert_array_equal([row["t_video_s"] for row in core.frames], expected)
    assert core.removed_voxels[0]["t_video_s"] == 100.1 - 100.0
    assert core.removed_voxels[0]["frame_idx_global"] == 1
    video = build_review_video(
        record,
        sources,
        tmp_path / "video",
        fps=receipt["fps"],
        frame_times_video=expected,
        clock_manifest=clock,
    )
    assert video["source_frame_count"] == 4
    assert video["source_time_bounds_s"] == [0.0, float(expected[-1])]
    assert video["duration_s"] == float(expected[-1])
    assert video["output_frame_count"] == 14
    assert (video["output_frame_count"] - 1) / video["fps"] >= expected[-1]
    assert all(
        video["validation"][key]
        for key in (
            "decode_ok",
            "color_order_check_passed",
            "frame_count_within_codec_tolerance",
            "fps_within_tolerance",
        )
    )
    assert before == {p.name: sha256_file(p) for p in list_hdf5_paths(raw)}
    assert sorted(p.name for p in raw.iterdir()) == sorted(before)


@pytest.mark.parametrize(
    "times", [[1.0, 1.0], [2.0, 1.0], [1.0, float("nan")], [1.0, float("inf")]]
)
def test_duplicate_reversed_or_nonfinite_native_times_rejected(tmp_path, times):
    raw = tmp_path / "raw"
    raw.mkdir()
    _chunk(raw / "chunk.hdf5", times)
    with pytest.raises(ValueError, match="strictly increasing"):
        generate_hdf5_alignment(raw, tmp_path / "output")
    assert not (tmp_path / "output/alignment.csv").exists()


@pytest.mark.parametrize("later", [[1.0, 3.0], [2.0, 3.0]])
def test_overlapping_sessions_or_duplicate_boundary_rejected(tmp_path, later):
    raw = tmp_path / "raw"
    raw.mkdir()
    _chunk(raw / "one.h5", [0.0, 2.0])
    _chunk(raw / "two.hdf5", later)
    with pytest.raises(ValueError, match="Overlapping or duplicate"):
        generate_hdf5_alignment(raw, tmp_path / "output")


@pytest.mark.parametrize("problem", ["corrupt", "missing_time", "missing_images", "count"])
def test_unreadable_or_partial_chunks_never_silently_skipped(tmp_path, problem):
    raw = tmp_path / "raw"
    raw.mkdir()
    _chunk(raw / "good.hdf5", [0.0, 1.0])
    if problem == "corrupt":
        (raw / "bad.hdf5").write_bytes(b"not an HDF5 file")
    else:
        _chunk(raw / "bad.hdf5", [2.0, 3.0])
        with h5py.File(raw / "bad.hdf5", "r+") as handle:
            if problem == "missing_time":
                del handle["data/time"]
            elif problem == "missing_images":
                del handle["data/l_img"]
            else:
                del handle["data/time"]
                handle.create_dataset("data/time", data=[2.0])
    with pytest.raises((OSError, ValueError)):
        generate_hdf5_alignment(raw, tmp_path / "output")
    assert not (tmp_path / "output/alignment.csv").exists()


@pytest.mark.parametrize(
    "change",
    [
        "csv",
        "source",
        "extra_source",
        "missing_receipt",
        "wrong_directory",
        "frame_identity",
        "duration",
    ],
)
def test_generated_clock_cannot_be_rebound_or_silently_downgraded(tmp_path, change):
    raw, record, sources, receipt = _fixture(tmp_path)
    rows, _ = read_hdf5_frame_inventory(raw)
    path = Path(receipt["alignment_path"])
    duration = receipt["duration_s"]
    if change == "csv":
        path.write_text(path.read_text().replace("100.17", "100.18"))
    elif change == "source":
        with h5py.File(raw / "z_early.H5", "r+") as handle:
            handle["data/l_img"][0, 0, 0, 0] = 42
    elif change == "extra_source":
        with h5py.File(raw / "extra.h5", "w") as handle:
            handle.create_group("metadata")
    elif change == "missing_receipt":
        Path(receipt["manifest_path"]).unlink()
    elif change == "wrong_directory":
        raw = tmp_path / "other"
        raw.mkdir()
    elif change == "frame_identity":
        rows[0]["source_frame_idx"] = 1
    elif change == "duration":
        duration += 1.0
    with pytest.raises((ValueError, FileNotFoundError)):
        resolve_frame_clock(
            rows, path, raw_run_dir=raw, global_run_id=record.global_run_id, duration_s=duration
        )


def test_generated_video_rejects_wrong_clock_and_fps(tmp_path):
    pytest.importorskip("cv2")
    _, record, sources, receipt = _fixture(tmp_path)
    core = extract_core_frames(record, sources, project_root=ROOT)
    values = np.array([row["t_video_s"] for row in core.frames])
    clock = core.manifest["clock_alignment"]
    for times, fps in ((values * 2, 10.0), (values, 5.0)):
        with pytest.raises(ValueError, match="differs from its source receipt"):
            build_review_video(
                record,
                sources,
                tmp_path / "video",
                fps=fps,
                frame_times_video=times,
                clock_manifest=clock,
            )


def test_no_source_writes_or_existing_output_overwrites(tmp_path):
    raw, _, _, receipt = _fixture(tmp_path)
    before = sha256_file(Path(receipt["alignment_path"]))
    with pytest.raises(FileExistsError):
        generate_hdf5_alignment(raw, Path(receipt["alignment_path"]).parent)
    for output in (raw, raw / "derived"):
        with pytest.raises(ValueError, match="outside the source"):
            generate_hdf5_alignment(raw, output)
    assert before == sha256_file(Path(receipt["alignment_path"]))


def test_verified_native_clock_is_accepted_with_explicit_model_limit(tmp_path, monkeypatch):
    _, record, sources, _ = _fixture(tmp_path)
    core = extract_core_frames(record, sources, project_root=ROOT)
    frames = []
    for native in core.frames:
        row = dict.fromkeys(SOURCE_COLUMNS)
        row.update(native, frame_removed_voxels=0, frame_removed_mm3=0, frame_removed_mm3_cum=0)
        frames.append(row)
    bundle = tmp_path / "stub_bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text("no model: this test follows the no-removal rule")
    stats = {
        "Columns": FEATURES,
        "Log1pColumns": REMOVAL,
        "Medians": [0.0] * 48,
        "Means": [0.0] * 48,
        "Std": [1.0] * 48,
    }
    manifest = {
        "model_id": "test_no_removal",
        "sha256": {
            "model.pt": "not loaded",
            "neural_preprocessing.json": "test statistics",
        },
    }
    monkeypatch.setattr(phase_detection, "load_bundle", lambda _: (bundle, manifest, stats))
    geometry = {
        "schema": "r4_anatomy_model_geometry_v1",
        "global_run_id": record.global_run_id,
        "contract_validation": "fixture",
        "recording_geometry_verified": False,
    }
    clock = core.manifest["clock_alignment"]
    result = phase_detection.infer_phases(
        frames,
        record,
        bundle_dir=bundle,
        output_dir=tmp_path / "phase",
        geometry_profile=geometry,
        clock_manifest=clock,
    )
    assert result.manifest["native_generated_clock"] is True
    assert "not been established" in result.manifest["clock_generalization_limit"]
    assert result.manifest["recording_geometry_verified"] is False
    assert result.manifest["reference_labels_used"] is False
    assert all(row["phase"] == "pre_drilling" for row in result.frames)
    assert result.manifest["runtime"] is None  # No neural forward or fitting.
    with pytest.raises(ValueError, match="verified source"):
        phase_detection.infer_phases(
            frames,
            record,
            bundle_dir=bundle,
            output_dir=tmp_path / "phase_rejected",
            geometry_profile=geometry,
            clock_manifest=dict(clock, native_correspondence_verified=False),
        )
