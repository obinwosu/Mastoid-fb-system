"""Meaningful boundary and missingness checks for the frozen signal input contract."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system import phase_detection  # noqa: E402
from evidence_system.phase_detection import assign_intervals  # noqa: E402
from evidence_system.phase_features import (  # noqa: E402
    FEATURES,
    REMOVAL,
    SOURCE_COLUMNS,
    extract_features,
    transform_features,
)


def frame(t, removed=0, clearance=None):
    row = dict.fromkeys(SOURCE_COLUMNS)
    row.update(t_video_s=t, frame_removed_voxels=removed, frame_removed_mm3=removed,
               frame_removed_mm3_cum=removed)
    row["dist_FacialNerve_mm"] = clearance
    row["voxel_min_FacialNerve_mm"] = clearance if removed else None
    return row


def test_open_left_window_and_removed_region_are_distinct_from_tip():
    rows = [frame(0, 4, 1), frame(4, 2, 3), frame(5, 0, 8)]
    raw, receipts = extract_features(rows, [5, 7, 40])
    idx = {name: i for i, name in enumerate(FEATURES)}
    assert raw[0, idx["source_removed_sum_5s"]] == 2
    assert raw[0, idx["clearance_FacialNerve_current"]] == 8
    assert raw[0, idx["removed_clearance_FacialNerve_cumulative_min"]] == 1
    assert raw[0, idx["removed_clearance_FacialNerve_min_5s"]] == 3
    assert np.isnan(raw[1, idx["clearance_FacialNerve_current"]])
    assert np.isnan(raw[2, idx["source_removed_sum_30s"]])
    assert raw[2, idx["removed_clearance_FacialNerve_cumulative_min"]] == 1
    assert receipts[0]["source_start_row_5s"] == 1
    assert not receipts[1]["current_fresh"]


def test_preprocessing_keeps_missing_flags_and_stored_statistics():
    stats = {"Columns": FEATURES, "Log1pColumns": REMOVAL,
             "Medians": [2] * 48, "Means": [1] * 48, "Std": [2] * 48}
    raw = np.zeros((1, 48))
    raw[0, 0] = np.expm1(3)
    raw[0, 3] = np.nan
    x = transform_features(raw, stats)
    assert x.shape == (1, 96) and x.dtype == np.float32
    assert x[0, 0] == 1
    assert x[0, 3] == 0.5 and x[0, 51] == 1
    assert x[0, 48] == 0


def test_schema_clock_and_negative_removal_fail_explicitly():
    with pytest.raises(ValueError, match="clock"):
        extract_features([frame(2), frame(1)], [2])
    row = frame(1)
    del row["dist_Incus_mm"]
    with pytest.raises(ValueError, match="dist_Incus"):
        extract_features([row], [1])


def test_first_removal_gate_latches_and_repeated_phases_keep_unique_intervals():
    rows = [frame(t, int(t == 0.5)) for t in (0, 0.5, 1, 2, 3, 4)]
    probabilities = np.array([[0.8, 0.1, 0.1], [0.1, 0.8, 0.1], [0.8, 0.1, 0.1], [0.1, 0.1, 0.8]])
    result, intervals = assign_intervals(
        rows, SimpleNamespace(global_run_id="synthetic:sample"),
        np.array([1, 2, 3, 4]), probabilities, 0.5, 4,
    )
    assert [r["model_phase"] for r in result] == [
        "predrilling", "early_phase", "early_phase", "antrum", "early_phase", "incus",
    ]
    assert len({r["phase_id"] for r in intervals}) == 5
    assert result[1]["phase_confidence"] == 0.8
    assert intervals[0]["annotation_end_s"] == 0.5
    assert all(r["phase_annotation_id"] is None for r in result)


def test_no_removal_is_entirely_rule_based_without_model_predictions():
    result, intervals = assign_intervals(
        [frame(0), frame(1)], SimpleNamespace(global_run_id="synthetic:sample"),
        np.array([]), np.empty((0, 3)), None, 1,
    )
    assert len(intervals) == 1
    assert all(r["phase"] == "pre_drilling" for r in result)
    assert all(r["phase_confidence"] is None for r in result)


def test_model_intervals_are_retained_across_gaps_in_native_frame_observations():
    _, intervals = assign_intervals(
        [frame(0), frame(0.5, 1), frame(4)],
        SimpleNamespace(global_run_id="synthetic:sample"),
        np.array([1, 2, 3, 4]), np.eye(3)[[0, 1, 0, 2]], 0.5, 4,
    )
    assert len(intervals) == 5
    assert intervals[2]["model_phase"] == "antrum"
    assert intervals[2]["observed_frame_count"] == 0
    assert intervals[2]["observed_start_s"] is None


def test_real_model_requires_training_clock_binding(monkeypatch, tmp_path):
    monkeypatch.setattr(phase_detection, "load_bundle", lambda _: (tmp_path, {}, {}))
    record = SimpleNamespace(global_run_id="synthetic:clock_contract")
    geometry = {"schema": "r4_signal_geometry_v1", "global_run_id": record.global_run_id,
                "contract_validation": "configuration consistency"}
    clock = {"configured_global_run_id": record.global_run_id,
             "frame_count": 1, "source": "native_relative_time"}
    with pytest.raises(ValueError, match="native HDF5 elapsed time"):
        phase_detection.infer_phases(
            [frame(0)], record, bundle_dir=tmp_path, output_dir=tmp_path,
            geometry_profile=geometry, clock_manifest=clock,
        )
    clock["configured_global_run_id"] = "synthetic:different"
    with pytest.raises(ValueError, match="not bound"):
        phase_detection.infer_phases(
            [frame(0)], record, bundle_dir=tmp_path, output_dir=tmp_path,
            geometry_profile=geometry, clock_manifest=clock,
        )


def test_inference_worker_uses_current_interpreter_and_imports_local_source(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")
    metadata = {"model_id": "generated_fixture", "sha256": {
        "model.pt": "fixture-checkpoint", "neural_preprocessing.json": "fixture-preprocessing",
    }}
    stats = {"Columns": FEATURES, "Log1pColumns": REMOVAL,
             "Medians": [0] * 48, "Means": [0] * 48, "Std": [1] * 48}
    monkeypatch.setattr(phase_detection, "load_bundle", lambda _: (bundle, metadata, stats))
    expected = np.asarray([[.8, .1, .1], [.1, .8, .1]], dtype=np.float32)
    calls = []

    def run_worker(command, **kwargs):
        calls.append(command)
        assert command[:3] == [sys.executable, "-m", "evidence_system.phase_worker"]
        source_root = str(Path(phase_detection.__file__).resolve().parents[1])
        assert kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0] == source_root
        assert kwargs["capture_output"] and kwargs["text"]
        inputs = np.load(command[command.index("--input") + 1], allow_pickle=False)
        assert inputs.shape == (2, 96) and np.isfinite(inputs).all()
        output = Path(command[command.index("--output") + 1])
        np.save(output, expected, allow_pickle=False)
        output.with_suffix(".json").write_text(json.dumps({"trained": False}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(phase_detection, "subprocess", SimpleNamespace(run=run_worker))
    record = SimpleNamespace(global_run_id="synthetic:current_environment")
    rows = [frame(0), frame(.5, 2, 1), frame(1, 0, 1), frame(2, 0, 1)]
    result = phase_detection.infer_phases(
        rows, record, bundle_dir=bundle, output_dir=tmp_path / "result",
        geometry_profile={"schema": "synthetic_signal_geometry_v1",
                          "global_run_id": record.global_run_id, "contract_validation": "fixture"},
        clock_manifest={"configured_global_run_id": record.global_run_id, "frame_count": len(rows),
                        "source": "explicit_frame_alignment", "duration_s": 2.},
    )
    assert len(calls) == 1
    assert [row["model_phase"] for row in result.frames] == ["predrilling", "early_phase", "early_phase", "antrum"]
    assert "phase_python" not in json.dumps(result.manifest)
    np.testing.assert_array_equal(np.load(tmp_path / "result/probabilities.npy"), expected)
