"""Mathematical and source-boundary checks for native-time motion comparisons."""

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.coaching_plan import build_session_coaching_plan
from evidence_system.motion_comparison import (
    DEFAULT_PROTOCOL,
    LEGACY_PROTOCOL,
    artifact_sha256,
    build_expert_reference,
    build_motion_windows,
    compare_motion_windows,
    validate_expert_reference,
    validate_window_build,
    write_motion_artifact,
)


def provenance(predicted=False):
    return {
        "pose_source": "canonical_vision_pose",
        "position_unit": "m",
        "phase_source_kind": "model_phase_prediction" if predicted else "manual_frame_phase",
        "phase_model_sha256": "c" * 64 if predicted else None,
        "clock_source": "generated_native_clock",
        "source_artifacts": [
            {"kind": "frames", "path": "generated_frames.csv", "sha256": "a" * 64},
            {"kind": "phases", "path": "generated_phases.json", "sha256": "b" * 64},
        ],
    }


def frames(speed=1.0, seconds=7, phase="antrum", *, origin=0, video_scale=1, video_offset=0):
    return [
        {
            "t_abs": origin + i / 4,
            "t_video_s": video_offset + video_scale * i / 4,
            "drill_x": speed * i / 4000,
            "drill_y": 0.0,
            "drill_z": 0.0,
            "phase": phase,
            "phase_id": f"phase:{phase}",
            "frame_removed_mm3": 0.25 if i else 0.0,
            "is_drilling_frame": False,
        }
        for i in range(int(seconds * 4) + 1)
    ]


def build(
    speed=1.0,
    seconds=7,
    *,
    run="case",
    participant="current",
    phase="antrum",
    rows=None,
    predicted=False,
):
    # Preserve the historical one-second-stride regression cases explicitly.
    # Default non-overlapping behavior is covered in test_nonoverlap_motion.py.
    return build_motion_windows(
        frames(speed, seconds, phase) if rows is None else rows,
        global_run_id=run,
        participant_id=participant,
        provenance=provenance(predicted),
        protocol=LEGACY_PROTOCOL,
    )


def reference(speeds=(10.0, 20.0, 30.0), phase="antrum"):
    return build_expert_reference(
        [
            build(speed, run=f"expert:{i}", participant=f"expert{i}", phase=phase)
            for i, speed in enumerate(speeds)
        ],
        expert_participant_ids=[f"expert{i}" for i in range(len(speeds))],
        provenance={"expert_definition": "Generated expert identities for numerical tests only"},
    )


def test_physical_speed_is_path_over_valid_native_time_not_video_time():
    result = build(rows=frames(2.0, video_scale=2, video_offset=137, origin=1000))
    first = result["windows"][0]
    assert first["path_length_mm"] == pytest.approx(10)
    assert first["valid_duration_s"] == 5
    assert first["speed_mm_s"] == pytest.approx(2)
    assert first["coverage_fraction"] == 1
    assert first["t_start_s"] == 137 and first["t_end_s"] == 147
    assert first["removed_mm3"] == 5 and first["removal_rate_mm3_s"] == 1
    assert first["removal_coverage_fraction"] == 1
    validate_window_build(result)


def test_long_gap_never_becomes_path_or_pause_and_removal_keeps_full_window_denominator():
    rows = frames(1.0, seconds=5)
    rows = [r for r in rows if r["t_abs"] <= 1 or r["t_abs"] >= 4]
    for row in rows:
        if row["t_abs"] >= 4:
            row["drill_x"] += 1000
    rows[5]["frame_removed_mm3"] = 100
    window = build(rows=rows)["windows"][0]
    assert window["valid_duration_s"] == 2
    assert window["coverage_fraction"] == 0.4
    assert window["path_length_mm"] == pytest.approx(2, abs=1e-8)
    assert window["speed_mm_s"] == pytest.approx(1, abs=1e-8)
    assert window["stationary_duration_s"] == 0
    assert window["excluded_gap_removal_mm3"] == 100
    assert window["removal_rate_mm3_s"] == 0.4
    assert not window["eligible"] and not window["removal_eligible"]


def test_missing_pose_reduces_coverage_while_observed_stationary_is_retained():
    rows = frames(0, seconds=5)
    for row in rows:
        row["frame_removed_mm3"] = 0
    stationary = build(rows=rows)["windows"][0]
    assert stationary["speed_mm_s"] == 0 and stationary["stationary_fraction"] == 1
    assert stationary["eligible"] and stationary["removal_rate_mm3_s"] == 0
    rows[10]["drill_x"] = None
    missing = build(rows=rows)["windows"][0]
    assert missing["valid_duration_s"] == 4.5 and missing["coverage_fraction"] == 0.9
    assert missing["stationary_duration_s"] == 4.5
    moving = frames(2, seconds=5)
    for row in moving:
        row["frame_removed_mm3"] = 0
    assert build(rows=moving)["windows"][0]["stationary_fraction"] == 0


def test_partial_short_segments_are_clipped_without_changing_constant_speed():
    rows = [
        {
            **frames(1, 0)[0],
            "t_abs": i * 0.24,
            "t_video_s": i * 0.24,
            "drill_x": i * 0.24 / 1000,
            "frame_removed_mm3": 0.24,
        }
        for i in range(31)
    ]
    windows = build(rows=rows)["windows"]
    assert len(windows) == 3
    assert windows[1]["valid_duration_s"] == pytest.approx(5)
    assert windows[1]["path_length_mm"] == pytest.approx(5)
    assert windows[1]["speed_mm_s"] == pytest.approx(1)


def three_phase_rows(speed=1):
    rows = []
    for index, phase in enumerate(("early_surface", "antrum", "incus")):
        for row in frames(speed, phase=phase):
            row["t_abs"] += index * 7.25
            row["t_video_s"] += index * 7.25
            rows.append(row)
    return rows


def test_all_three_phases_do_not_cross_boundaries_or_include_predrilling():
    rows = [
        {**row, "t_abs": row["t_abs"] - 7.25, "t_video_s": row["t_video_s"] - 7.25}
        for row in frames(1, phase="pre_drilling")
    ] + three_phase_rows()
    result = build(rows=rows)
    assert [w["phase"] for w in result["windows"]] == ["early_phase"] * 3 + ["antrum"] * 3 + [
        "incus"
    ] * 3
    assert {w["phase_id"] for w in result["windows"]} == {
        "phase:early_surface",
        "phase:antrum",
        "phase:incus",
    }
    for window in result["windows"]:
        start, end = window["source_start_row"], window["source_end_row"]
        assert rows[start]["phase_id"] == rows[end]["phase_id"] == window["phase_id"]


def test_missing_phase_ids_derive_only_contiguous_known_labels_with_repeat_identity():
    rows = three_phase_rows()
    for row in rows:
        row.pop("phase_id")
    for row in rows[-29:]:
        row["phase"] = "early_surface"
    result = build(rows=rows)
    assert result["derived_phase_id_row_count"] == len(rows)
    assert len({w["phase_id"] for w in result["windows"]}) == 3
    assert result["windows"][0]["phase_id"] != result["windows"][-1]["phase_id"]
    assert result["input_rows_sha256"] == build(rows=deepcopy(rows))["input_rows_sha256"]


@pytest.mark.parametrize(
    "mutation", ["duplicate_time", "reversed_video", "negative_removal", "missing_field"]
)
def test_bad_source_contract_rejected(mutation):
    rows = frames()
    if mutation == "duplicate_time":
        rows[1]["t_abs"] = rows[0]["t_abs"]
    elif mutation == "reversed_video":
        rows[1]["t_video_s"] = -1
    elif mutation == "negative_removal":
        rows[1]["frame_removed_mm3"] = -1
    else:
        del rows[1]["drill_x"]
    with pytest.raises(ValueError):
        build(rows=rows)


def test_equal_participant_weight_prevents_prolific_expert_dominating_percentile():
    builds = [build(1, run=f"a:{i}", participant="a") for i in range(10)]
    builds += [build(10, run="b:1", participant="b"), build(100, run="c:1", participant="c")]
    ref = build_expert_reference(
        builds, expert_participant_ids=["a", "b", "c"], provenance={"expert_definition": "fixture"}
    )
    result = compare_motion_windows(build(10), ref)
    comparison = result["windows"][0]["comparisons"]["speed_mm_s"]
    assert comparison["reference_recording_n"] == 12
    assert comparison["reference_participant_n"] == 3
    assert comparison["percentile"] == pytest.approx(50)
    assert comparison["reference_median"] == pytest.approx(10)


def test_equal_recording_weight_prevents_long_recording_dominating_same_participant():
    builds = [
        build(1, seconds=55, run="a:long", participant="a"),
        build(9, run="a:short", participant="a"),
        build(5, run="b", participant="b"),
        build(5, run="c", participant="c"),
    ]
    ref = build_expert_reference(
        builds, expert_participant_ids=["a", "b", "c"], provenance={"expert_definition": "fixture"}
    )
    result = compare_motion_windows(build(5), ref)
    assert result["windows"][0]["comparisons"]["speed_mm_s"]["percentile"] == pytest.approx(50)


def test_self_exclusion_removes_all_recordings_of_participant_and_reweights():
    ref = reference((10, 20, 30, 40))
    own_extra = build(1, run="expert:extra", participant="expert0")
    ref = build_expert_reference(
        [*ref["recordings"], own_extra],
        expert_participant_ids=ref["expert_participant_ids"],
        provenance={"expert_definition": "fixture"},
    )
    result = compare_motion_windows(build(1, run="expert:0", participant=None), ref)
    assert result["participant_id"] == "expert0"
    assert set(result["reference_summary"]["excluded_recording_ids"]) == {
        "expert:0",
        "expert:extra",
    }
    assert result["per_phase"]["antrum"]["speed_mm_s"]["participant_n"] == 3
    assert result["windows"][0]["comparisons"]["speed_mm_s"]["reference_median"] == pytest.approx(
        30
    )
    with pytest.raises(ValueError, match="identity"):
        compare_motion_windows(build(run="expert:0", participant="someone_else"), ref)


def test_insufficient_subjects_or_window_coverage_cannot_produce_coaching():
    result = compare_motion_windows(build(0), reference((10, 20)))
    assert not result["available"] and result["episodes"] == []
    assert (
        result["windows"][0]["comparisons"]["speed_mm_s"]["reason"]
        == "insufficient_expert_participants"
    )
    assert compare_motion_windows(build(), None)["reason"] == "reference_not_configured"
    rows = frames(0)
    for row in rows[5:24]:
        row["drill_x"] = None
    assert compare_motion_windows(build(rows=rows), reference())["episodes"] == []


def test_three_overlapping_lows_create_seven_second_review_not_fifteen_seconds():
    result = compare_motion_windows(build(1, predicted=True), reference())
    assert len(result["episodes"]) == 1
    episode = result["episodes"][0]
    assert episode["window_count"] == 3
    assert episode["native_end_s"] - episode["native_start_s"] == 7
    assert result["reference_summary"]["query_phase_source_kind"] == "model_phase_prediction"
    assert result["reference_summary"]["reference_phase_sources"] == ["manual_frame_phase"]
    assert compare_motion_windows(build(1, seconds=6), reference())["episodes"] == []


def test_tied_zero_reference_is_not_low_and_display_jump_suppresses_review():
    zero = compare_motion_windows(build(0), reference((0, 0, 0)))
    assert zero["windows"][0]["comparisons"]["speed_mm_s"]["percentile"] == 50
    assert zero["windows"][0]["comparisons"]["speed_mm_s"]["relative_deficit"] is None
    assert zero["episodes"] == []
    rows = frames(1)
    for row in rows:
        if row["t_abs"] >= 3:
            row["t_video_s"] += 100
    result = compare_motion_windows(build(rows=rows), reference())
    assert result["available"] and result["episodes"] == []
    assert all(not w["display_clock_valid"] for w in result["windows"])


def test_all_phase_coaching_is_descriptive_and_preserves_each_review_interval():
    experts = [
        build(rows=three_phase_rows(10 * (i + 1)), run=f"expert{i}", participant=f"expert{i}")
        for i in range(3)
    ]
    ref = build_expert_reference(
        experts,
        expert_participant_ids=[f"expert{i}" for i in range(3)],
        provenance={"expert_definition": "fixture"},
    )
    result = compare_motion_windows(build(rows=three_phase_rows(1)), ref)
    plan = build_session_coaching_plan([], [], {}, None, motion_comparison=result)
    assert [item["feedback_id"] for item in plan["items"]] == ["coaching:motion_reference"]
    assert all(item["evidence_status"] == "descriptive_cohort_comparison" for item in plan["items"])
    assert all(
        "increase drilling speed" not in item["recommended_action"] for item in plan["items"]
    )
    assert [clip["phase"] for clip in plan["items"][0]["review_clips"]] == [
        "early_phase",
        "antrum",
        "incus",
    ]
    assert "not a skill score" in plan["items"][0]["claim_boundary"]


def test_version_hash_and_protocol_mismatch_fail_closed_and_old_phase_means_not_windows(tmp_path):
    current = build()
    path = tmp_path / "motion.json"
    write_motion_artifact(path, current)
    validate_window_build(json.loads(path.read_text()))
    changed = deepcopy(current)
    changed["windows"][0]["speed_mm_s"] = 900
    with pytest.raises(ValueError, match="digest"):
        validate_window_build(changed)
    with pytest.raises(ValueError, match="phase means"):
        validate_expert_reference(
            {"phases": {"antrum": {"experienced_q25": {"mean_stroke_length": 1}}}}
        )
    policy = {**DEFAULT_PROTOCOL, "min_coverage": 0.9}
    changed = build_motion_windows(
        frames(), global_run_id="different_protocol", provenance=provenance(), protocol=policy
    )
    with pytest.raises(ValueError, match="protocols"):
        compare_motion_windows(changed, reference())


def test_extra_feedback_camera_and_stroke_fields_cannot_affect_measurements_or_input_digest():
    rows = frames()
    baseline = build(rows=rows)
    for row in rows:
        row.update(
            camera_x=999, feedback="not input", stroke_id="unrelated", tip_visibility="unknown"
        )
    changed = build(rows=rows)
    assert baseline["windows"] == changed["windows"]
    assert baseline["input_rows_sha256"] == changed["input_rows_sha256"]
    assert baseline["artifact_sha256"] == artifact_sha256(baseline)
