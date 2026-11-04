"""Length enrichment preserves archive authority and missing-stroke behavior."""

import json
import sqlite3
from copy import deepcopy

import pytest
from test_review_frames import bound_case, dump, reference_for, sha

from evidence_system.motion_comparison import (
    LEGACY_PROTOCOL,
    add_stroke_length,
    artifact_sha256,
    build_motion_windows,
    validate_expert_reference,
)


def with_strokes(service, frames, *, update_database=True):
    rows = [
        dict(
            row,
            is_drilling_frame=True,
            stroke_id=index // 10,
            stroke_end_flag=(index % 10 == 9 or index == len(frames) - 1),
        )
        for index, row in enumerate(frames)
    ]
    if update_database:
        with sqlite3.connect(service.retriever.db_path) as connection:
            for index, row in enumerate(rows):
                connection.execute(
                    "UPDATE frames SET observation_json=? WHERE frame_idx=?",
                    (json.dumps(row), index),
                )
    return rows


def length_reference(service, frames, *, protocol=LEGACY_PROTOCOL):
    old = reference_for(service, frames, protocol=protocol)
    reference = deepcopy(old)
    for index, record in enumerate(reference["recordings"]):
        rows = [{**row, "drill_x": row["drill_x"] * 4} for row in frames]
        reference["recordings"][index] = add_stroke_length(record, rows)
    reference["artifact_sha256"] = artifact_sha256(reference)
    validate_expert_reference(reference)
    dump(service.cohort_benchmark_path, reference)
    return old, reference


def save_sidecar(service, frames, stages, *, length, protocol=LEGACY_PROTOCOL):
    run = service.retriever.get_run_summary("case")["run"]
    context = service._bound_review_context(run)
    files = {
        "frames": stages / "05_strokes/frame_observations_complete.csv",
        "phases": stages / "01_core_frames/phase_intervals.csv",
        "clock": context["core_path"],
    }
    files["frames"].write_text("synthetic frame artifact")
    files["phases"].write_text("synthetic phase artifact")
    provenance = {
        "pose_source": "canonical_vision_pose",
        "position_unit": "m",
        "phase_source_kind": "model_phase_prediction",
        "phase_model_sha256": "a" * 64,
        "clock_source": context["clock"]["source"],
        "source_artifacts": [
            {"kind": kind, "path": str(path), "sha256": sha(path)} for kind, path in files.items()
        ],
    }
    saved = build_motion_windows(
        frames,
        global_run_id="synthetic:case",
        participant_id=run.get("participant_id") or None,
        provenance=provenance,
        protocol=protocol,
    )
    if length:
        saved = add_stroke_length(saved, frames)
    path = stages / "09_motion_comparison/motion_windows.json"
    dump(path, saved)
    return path, saved


@pytest.mark.parametrize("sidecar_kind", ["absent", "old", "length"])
def test_length_service_uses_current_whole_strokes_and_keeps_frequency_saved_only(
    tmp_path, sidecar_kind
):
    service, frames, _, stages = bound_case(tmp_path)
    frames = with_strokes(service, frames)
    old_reference, _ = length_reference(service, frames)
    path = None
    if sidecar_kind != "absent":
        path, _ = save_sidecar(service, frames, stages, length=sidecar_kind == "length")
    before = path.read_bytes() if path else None
    bootstrap = service.bootstrap()
    comparison = bootstrap["motion_comparison"]
    assert comparison["available"]
    first = comparison["windows"][0]
    assert first["mean_stroke_length_mm"] == pytest.approx(0.9)
    assert first["comparisons"]["mean_stroke_length_mm"]["status"] == "compared"
    assert first["comparisons"]["mean_stroke_length_mm"]["reference_median"] == pytest.approx(3.6)
    assert first["comparisons"]["speed_mm_s"]["observed"] == pytest.approx(1.0)
    assert "stroke_frequency_hz" not in first["comparisons"]
    assert "stroke_rate_per_s" not in first["comparisons"]
    assert comparison["length_episodes"]
    for episode in comparison["length_episodes"]:
        anchor = frames[episode["anchor_frame_idx_global"]]
        assert anchor["stroke_end_flag"]
        assert anchor["t_video_s"] == episode["anchor_t_s"]
        assert episode["t_start_s"] <= episode["anchor_t_s"] <= episode["t_end_s"]
    length_cards = [
        i
        for i in bootstrap["coaching_plan"]["items"]
        if i["feedback_id"] == "coaching:short_strokes"
    ]
    assert length_cards and all(
        c["keyframe_available"] for i in length_cards for c in i["review_clips"]
    )
    if path:
        assert comparison["current_source"].startswith("bound_pipeline_sidecar")
        assert path.read_bytes() == before  # Archive enrichment is in memory only.
    dump(service.cohort_benchmark_path, old_reference)
    comparison = service.bootstrap()["motion_comparison"]
    assert comparison["available"]
    assert "mean_stroke_length_mm" not in comparison["windows"][0]["comparisons"]


def test_resigned_length_sidecar_metadata_still_must_match_current_database(tmp_path):
    service, frames, _, stages = bound_case(tmp_path)
    frames = with_strokes(service, frames)
    length_reference(service, frames)
    path, saved = save_sidecar(service, frames, stages, length=True)
    assert service.bootstrap()["motion_comparison"]["available"]
    forged = deepcopy(saved)
    forged["stroke_length_metadata"]["input_sha256"] = "f" * 64
    forged["artifact_sha256"] = artifact_sha256(forged)
    dump(path, forged)
    assert not service.bootstrap()["motion_comparison"]["available"]


def test_missing_archived_stroke_fields_leave_speed_comparison_available(tmp_path):
    service, frames, _, _ = bound_case(tmp_path)
    reference_rows = with_strokes(service, frames, update_database=False)
    length_reference(service, reference_rows)
    result = service.bootstrap()["motion_comparison"]
    assert result["available"]
    assert result["stroke_length_unavailable_reason"]
    first = result["windows"][0]["comparisons"]
    assert first["speed_mm_s"]["status"] == "compared"
    assert first["mean_stroke_length_mm"]["status"] == "not_evaluable"
    assert first["mean_stroke_length_mm"]["observed"] is None
