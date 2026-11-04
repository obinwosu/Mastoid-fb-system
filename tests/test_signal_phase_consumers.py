"""Synthetic predicted-phase provenance, repeated intervals, and real video checks."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evidence_system.coaching_plan import build_session_coaching_plan  # noqa: E402
from evidence_system.events.builder import build_events, phase_interval_at  # noqa: E402
from evidence_system.frame_pipeline.core_frames import CoreFrameBuild  # noqa: E402
from evidence_system.frame_pipeline.strokes import StrokeBuild  # noqa: E402
from evidence_system.phase_detection import PhaseBuild, assign_intervals  # noqa: E402
from evidence_system.pipeline import build_evidence  # noqa: E402
from evidence_system.registry import get_run, load_registry  # noqa: E402
from evidence_system.relations.builder import build_relations  # noqa: E402
from evidence_system.retrieval.api import EvidenceRetriever  # noqa: E402
from evidence_system.review_video import build_review_video  # noqa: E402
from evidence_system.source_config import (  # noqa: E402
    ResolvedRunSources,
    SourceRoots,
    resolve_run_sources,
)
from evidence_system.storage.store import populate_evidence_store  # noqa: E402
from evidence_system.web import FeedbackService  # noqa: E402
from scripts.create_demo_data import create_demo_data  # noqa: E402


def _record():
    return get_run("DEMO_CASE_01", load_registry(ROOT / "config/examples/runs.example.csv"))


def _phases():
    names = ("pre_drilling", "antrum", "incus", "antrum")
    return [
        {
            "phase_id": f"interval:{i}",
            "phase": name,
            "model_phase": "predrilling" if name == "pre_drilling" else name,
            "phase_source": "model_signal_mstcnpp",
            "annotation_start_s": float(i),
            "annotation_end_s": float(i + 1),
        }
        for i, name in enumerate(names)
    ]


def test_repeated_phase_identity_survives_events_relations_store_and_retrieval(tmp_path):
    record = _record()
    phases = _phases()
    frames = [
        {
            "frame_evidence_id": f"{record.global_run_id}:frame:{i}",
            "frame_idx_global": i,
            "source_hdf5": "sample.hdf5",
            "source_frame_idx": i,
            "t_video_s": i + 0.25,
            "phase": phase["phase"],
            "phase_id": phase["phase_id"],
            "model_phase": phase["model_phase"],
            "phase_source": phase["phase_source"],
            "frame_removed_voxels": int(i > 0),
            "frame_removed_mm3": float(i > 0),
            "is_drilling_frame": i > 0,
            "voxel_min_EAC_mm": 2.0,
        }
        for i, phase in enumerate(phases)
    ]
    strokes = StrokeBuild(frames, [], [], [], {"ready_for_eventization": True})
    core = CoreFrameBuild(frames, [], phases, {})
    events = build_events(strokes, phases, record)
    assert len(events.events) == 3
    assert [event.attributes["phase_id"] for event in events.events] == [
        "interval:1",
        "interval:2",
        "interval:3",
    ]
    assert phase_interval_at(2.0, phases)["phase_id"] == "interval:2"
    relations = build_relations(events, record)
    occurs = {
        row.source_id: row.target_id
        for row in relations.relations
        if row.relation_type == "occurs_during"
    }
    assert [occurs[event.event_id] for event in events.events] == [
        "interval:1",
        "interval:2",
        "interval:3",
    ]
    assert all(
        row.target_id != "source:human_video_timepoint_annotation" for row in relations.relations
    )
    phase_file = tmp_path / "phase_manifest.json"
    phase_file.write_text(json.dumps({"source_kind": "model_phase_prediction"}))
    sources = ResolvedRunSources(tmp_path, tmp_path, phase_file, tmp_path / "absent.json")
    audit = {
        "hdf5": {"chunks": [{"file_name": "sample.hdf5", "path": "sample.hdf5", "size_bytes": 0}]},
        "phase_annotation": {"path": str(phase_file), "source_kind": "model_phase_prediction"},
        "edt": {"headers": []},
    }
    db = tmp_path / "evidence.sqlite3"
    manifest = populate_evidence_store(
        db,
        record=record,
        sources=sources,
        source_audit=audit,
        core=core,
        strokes=strokes,
        events=events,
        relations=relations,
        artifact_root=tmp_path,
        project_root=ROOT,
    )
    assert manifest["integrity_check"] == "ok"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT phase_id FROM frames ORDER BY frame_idx").fetchall() == [
            (f"interval:{i}",) for i in range(4)
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    retriever = EvidenceRetriever(db)
    summary = retriever.get_phase_summary(record.run_id, "antrum")
    assert summary["frame_count"] == 2
    assert summary["removed_volume_mm3"] == 2
    assert summary["event_counts"] == {"boundary_work": 2}
    assert summary["interval_count"] == 2
    assert summary["evidence_ids"] == ["interval:1", "interval:3"]
    assert "t_start_s" not in summary["phase"]  # No false continuous envelope.
    run = retriever.get_run_summary(record.run_id)
    assert run["predicted_drilling_phase_count"] == 3
    assert run["annotated_drilling_phase_count"] == 0
    assert len(retriever.compare_phases(record.run_id, ["antrum"])["phases"][0]["intervals"]) == 2


def test_coaching_links_every_antrum_interval_without_bridging_other_phases():
    phases = [
        {
            "phase_id": row["phase_id"],
            "phase_name": row["phase"],
            "t_start_s": row["annotation_start_s"],
            "t_end_s": row["annotation_end_s"],
        }
        for row in _phases()
    ]
    benchmark = {
        "phases": {
            "antrum": {
                "observed": {"mean_stroke_length": 1, "stroke_rate_per_s": 3},
                "experienced_q25": {"mean_stroke_length": 2},
                "experienced_q75": {"stroke_rate_per_s": 2},
            }
        }
    }
    item = build_session_coaching_plan([], phases, {}, benchmark)["items"][0]
    assert [(row["t_start_s"], row["t_end_s"]) for row in item["review_clips"]] == [
        (1.0, 2.0),
        (3.0, 4.0),
    ]
    assert item["t_start_s"] == 1.0 and item["t_end_s"] == 2.0


def _video_fixture(tmp_path):
    h5py = pytest.importorskip("h5py")
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    raw = tmp_path / "raw"
    raw.mkdir()
    path = raw / "sample.hdf5"
    images = np.zeros((3, 32, 32, 3), dtype=np.uint8)
    images[0] = [220, 30, 10]
    images[1] = [10, 40, 220]
    images[2] = [200, 50, 20]
    with h5py.File(path, "w") as handle:
        handle.create_dataset("data/time", data=[100.0, 101.0, 102.0])
        handle.create_dataset("data/l_img", data=images)
    with h5py.File(raw / "terminal.hdf5", "w") as handle:
        handle.create_group("metadata")
    return path, ResolvedRunSources(
        raw, tmp_path, tmp_path / "unused.csv", tmp_path / "absent.json"
    )


def test_actual_webm_uses_canonical_clock_native_order_and_bgr(tmp_path):
    path, sources = _video_fixture(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = build_review_video(
        _record(),
        sources,
        tmp_path / "video",
        fps=2,
        frame_times_video=[0.0, 2.0, 4.0],
        clock_manifest={"mode": "verified_fixture_clock", "duration_s": 4.5},
    )
    assert manifest["duration_s"] == 4.5  # Native duration is only two seconds.
    assert manifest["source_time_bounds_s"] == [0.0, 4.0]
    assert manifest["held_last_frame_after_source_s"] == 0.5
    assert manifest["output_frame_count"] == 10
    assert manifest["source_frame_count"] == 3
    assert manifest["clock_alignment"]["mode"] == "verified_fixture_clock"
    assert manifest["validation"]["decode_ok"]
    assert manifest["validation"]["color_order_check_passed"]
    assert manifest["validation"]["frame_count_within_codec_tolerance"]
    assert manifest["validation"]["fps_within_tolerance"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("times", [[0.0, 1.0], [0.0, float("nan"), 2.0], [0.0, 2.0, 1.0]])
def test_video_rejects_incomplete_or_invalid_mapped_clocks(tmp_path, times):
    _, sources = _video_fixture(tmp_path)
    with pytest.raises(ValueError, match="clock"):
        build_review_video(_record(), sources, tmp_path / "video", frame_times_video=times)


@pytest.mark.parametrize("removal_count", [0, 1, 17])
def test_sparse_removal_builds_database_video_feedback_and_preserves_stroke_clocks(
    tmp_path,
    removal_count,
):
    """Actual source/geometry/consumer path, with explicitly stubbed model outputs."""
    h5py = pytest.importorskip("h5py")
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    pytest.importorskip("scipy")
    fixture = create_demo_data(tmp_path / "demo")
    record = _record()
    with h5py.File(fixture["hdf5_file"], "r+") as handle:
        for name in ("voxel_time_stamp", "voxel_removed"):
            key = "voxels_removed/" + name
            values = handle[key][:removal_count]
            del handle[key]
            handle.create_dataset(key, data=values)
    bundle = tmp_path / "stub_bundle"
    bundle.mkdir()
    for name in ("manifest.json", "model.pt", "neural_preprocessing.json"):
        (bundle / name).write_text("Synthetic orchestration stub; no trained weights")
    clock = tmp_path / "clock.csv"
    clock.write_text(
        "frame_idx_global,video_time_s\n" + "".join(f"{i},{i * 0.2}\n" for i in range(24))
    )
    profiles = json.loads((ROOT / "config/examples/anatomy_profiles.example.json").read_text())
    profile = profiles["profiles"][record.anatomy_key]
    profile["phase_signal_geometry"] = {
        "schema": "synthetic_signal_geometry_v1",
        "global_run_id": record.global_run_id,
        "edt_profile": record.edt_profile,
        "actual_specimen": "synthetic_fixture",
        "calibration_recording_id": "synthetic_fixture",
        "rotation_source": "generated identity",
        "volume_quaternion_xyzw": [0.0, 0.0, 0.0, 1.0],
        "recorder_scale_m_per_simulator_unit": profile["meters_per_simulator_unit"],
        "edt_distance_scale_mm": profile["edt_distance_scale_mm"],
        "volume_dimensions_sim": profile["volume_dimensions_sim"],
        "geometry_quality": "synthetic",
    }
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps(profiles))
    sources = resolve_run_sources(
        record,
        SourceRoots(
            Path(fixture["output_root"]) / "raw",
            Path(fixture["output_root"]) / "edt",
            phase_model_bundle=bundle,
            frame_alignment_file=clock,
            phase_duration_s=5.0,
        ),
    )
    Path(fixture["phase_annotation_file"]).unlink()

    def fake_infer(frames, actual_record, **kwargs):
        onset = next((row["t_video_s"] for row in frames if row["frame_removed_voxels"] > 0), None)
        times = np.arange(1.0, 6.0)
        times = times[times >= onset] if onset is not None else times[:0]
        probabilities = np.tile([0.8, 0.1, 0.1], (len(times), 1))
        assigned, intervals = assign_intervals(
            frames,
            actual_record,
            times,
            probabilities,
            onset,
            5.0,
        )
        manifest = {
            "source_kind": "model_phase_prediction",
            "model_id": "synthetic_stub",
            "first_removal_s": onset,
            "reference_labels_used": False,
        }
        kwargs["output_dir"].mkdir(parents=True)
        (kwargs["output_dir"] / "manifest.json").write_text(json.dumps(manifest))
        return PhaseBuild(assigned, intervals, manifest)

    output = tmp_path / "out"
    with (
        patch("evidence_system.phase_detection.infer_phases", side_effect=fake_infer),
        patch.dict(os.environ, {"MASTOID_ANATOMY_PROFILES": str(profiles_path)}),
    ):
        result = build_evidence(
            record=record,
            sources=sources,
            result_dir=output,
            project_root=ROOT,
            build_video=True,
            video_fps=2,
        )
    assert result["frame_count"] == 24
    assert result["removed_voxel_count"] == removal_count
    assert result["store_integrity"] == "ok"
    manifest = json.loads((output / "stages/05_strokes/stroke_manifest.json").read_text())
    if removal_count < 7:
        assert result["stroke_count"] == 0
        assert manifest["segmentation"]["status"] == "insufficient_drilling_frames"
        assert manifest["ready_for_eventization"]
        assert not manifest["metrics_available"]
    with sqlite3.connect(output / "evidence.sqlite3") as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        observations = [
            json.loads(row[0])
            for row in connection.execute("SELECT observation_json FROM frames ORDER BY frame_idx")
        ]
        if removal_count == 0:
            assert all(row["model_phase"] == "predrilling" for row in observations)
        else:
            assert observations[2]["model_phase"] == "early_phase"
        if removal_count == 17:
            assert result["stroke_count"] > 0
            for (encoded,) in connection.execute("SELECT metrics_json FROM strokes"):
                interval = json.loads(encoded)["interval"]
                assert interval["boundary_start_video"] == pytest.approx(
                    2 * interval["boundary_start_abs"]
                )
                assert interval["boundary_end_video"] == pytest.approx(
                    2 * interval["boundary_end_abs"]
                )
                assert interval["video_duration_s"] == pytest.approx(2 * interval["duration_s"])
                assert interval["duration_clock"] == "native_HDF5_data/time"
    service = FeedbackService(
        mode="recommendations",
        db_path=output / "evidence.sqlite3",
        run_id=record.run_id,
        video_path=output / "review_video.webm",
        video_manifest_path=output / "review_video_manifest.json",
        ray_v3_dir=None,
        cohort_benchmark_path=None,
        available_runs=[],
        display_name="Synthetic fixture",
        interface_version="test",
    )
    assert service.bootstrap()["media"]["available"]
