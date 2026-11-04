"""Whole-phase arithmetic, source exclusions and participant-balanced distributions."""

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.motion_comparison import artifact_sha256
from evidence_system.phase_summary import (
    build_phase_expert_reference,
    build_phase_motion,
    compare_phase_motion,
    load_phase_reference,
    validate_phase_motion,
    validate_phase_reference,
)


def frames(seconds=7, speed=1.0, phase="antrum"):
    return [
        {
            "t_abs": 1000 + i / 4,
            "t_video_s": 30 + i / 4,
            "drill_x": speed * i / 4000,
            "drill_y": 0.0,
            "drill_z": 0.0,
            "phase": phase,
            "phase_id": f"phase:{phase}",
            "frame_removed_mm3": 0.25 if i else 0,
            "is_drilling_frame": True,
            "stroke_id": (max(i, 1) - 1) // 4,
            "stroke_end_flag": i > 0 and i % 4 == 0,
        }
        for i in range(int(seconds * 4) + 1)
    ]


def build(rows=None, *, speed=1.0, run="query", participant="query", recording_id=None):
    return build_phase_motion(
        frames(speed=speed) if rows is None else rows,
        global_run_id=run,
        participant_id=participant,
        provenance={
            "pose_source": "canonical_vision_pose",
            "position_unit": "m",
            "phase_source_kind": "manual_frame_phase",
            "clock_source": "generated_native_clock",
            "recording_id": recording_id,
            "source_artifacts": [
                {"kind": "frames", "path": "generated_frames.csv", "sha256": "a" * 64},
                {"kind": "phases", "path": "generated_phases.csv", "sha256": "b" * 64},
            ],
        },
    )


def reference(builds=None):
    builds = builds or [build(speed=i + 1, run=f"E{i}", participant=f"P{i}") for i in range(4)]
    return build_phase_expert_reference(
        builds,
        expert_participant_ids=sorted({row["participant_id"] for row in builds}),
        provenance={"expert_definition": "Synthetic numerical-test participants"},
    )


def test_entire_phase_includes_short_tail_and_weights_speed_by_time():
    rows = frames()
    for i, row in enumerate(rows):
        if i > 20:
            row["drill_x"] = (5 + 3 * (i / 4 - 5)) / 1000
            row["frame_removed_mm3"] = 0.5
        row["t_video_s"] = 200 + 2 * (row["t_abs"] - 1000)
    result = build(rows)
    phase = result["phases"][0]
    assert phase["duration_s"] == 7
    assert phase["intervals"][0]["t_end_s"] == 214
    assert phase["path_length_mm"] == pytest.approx(11)
    assert phase["metrics"]["speed_mm_s"]["value"] == pytest.approx(11 / 7)
    assert phase["removed_mm3"] == 9
    assert phase["metrics"]["removal_rate_mm3_s"]["value"] == pytest.approx(9 / 7)
    assert phase["stroke_end_count"] == 7
    assert phase["metrics"]["stroke_frequency_hz"]["value"] == 1
    validate_phase_motion(result)


def test_short_phase_is_measured_and_not_discarded_for_missing_five_second_window():
    phase = build(frames(seconds=2))["phases"][0]
    assert phase["duration_s"] == 2
    assert phase["metrics"]["speed_mm_s"]["eligible"]
    assert phase["metrics"]["speed_mm_s"]["value"] == pytest.approx(1)
    assert phase["eligible_stroke_count"] == 1
    assert phase["metrics"]["mean_stroke_length_mm"]["value"] == pytest.approx(1)


def test_repeated_phases_combine_separate_spans_without_bridging_another_phase():
    rows = frames(seconds=8)
    for i, row in enumerate(rows):
        if 9 <= i <= 23:
            row.update(phase="incus", phase_id="incus:1", drill_x=50 + i / 4000)
        elif i >= 24:
            row.update(phase_id="antrum:2", drill_x=100 + i / 4000)
    phases = {row["phase"]: row for row in build(rows)["phases"]}
    phase = phases["antrum"]
    assert phase["duration_s"] == 4
    assert len(phase["intervals"]) == 2
    assert phase["path_length_mm"] == pytest.approx(4)
    assert phase["metrics"]["speed_mm_s"]["value"] == pytest.approx(1)
    assert phases["incus"]["duration_s"] == 3.5


def test_long_gap_is_excluded_and_cannot_become_a_fast_movement():
    rows = [row for row in frames() if row["t_abs"] <= 1001 or row["t_abs"] >= 1004]
    rows[5]["frame_removed_mm3"] = 100
    for row in rows[5:]:
        row["drill_x"] += 100
    phase = build(rows)["phases"][0]
    assert phase["valid_duration_s"] == 4
    assert phase["metrics"]["speed_mm_s"]["value"] == pytest.approx(1)
    assert not phase["metrics"]["speed_mm_s"]["eligible"]
    assert phase["coverage_fraction"] == pytest.approx(4 / 7)
    assert phase["excluded_gap_removal_mm3"] == 100
    assert phase["metrics"]["removal_rate_mm3_s"]["value"] == pytest.approx(4 / 7)


def test_missing_strokes_do_not_hide_other_metrics_and_zero_is_not_missing():
    rows = frames(speed=0)
    for row in rows:
        del row["stroke_end_flag"]
        row["frame_removed_mm3"] = 0
    phase = build(rows)["phases"][0]
    assert phase["metrics"]["speed_mm_s"] == {"value": 0, "eligible": True, "reason": None}
    assert phase["metrics"]["removal_rate_mm3_s"]["value"] == 0
    assert phase["metrics"]["mean_stroke_length_mm"]["value"] is None
    assert not phase["metrics"]["stroke_frequency_hz"]["eligible"]
    assert phase["metrics"]["stroke_frequency_hz"]["reason"] == "missing_recorded_stroke_fields"


def test_nonfinite_segmentation_retains_counts_but_excludes_frequency_and_length():
    result = build(frames(speed=0))
    phase = result["phases"][0]
    assert result["stroke_source_status"] == "nonfinite_legacy_turn_angles"
    assert phase["stroke_end_count"] == 7
    assert phase["metrics"]["stroke_frequency_hz"]["value"] == 1
    assert not phase["metrics"]["stroke_frequency_hz"]["eligible"]
    assert phase["metrics"]["mean_stroke_length_mm"]["value"] is None


def test_complete_length_counts_strokes_once_and_excludes_forced_final_boundary():
    phase = build()["phases"][0]
    assert phase["eligible_stroke_ids"] == [0, 1, 2, 3, 4, 5]
    assert phase["metrics"]["mean_stroke_length_mm"]["value"] == pytest.approx((1 + 5 * 0.75) / 6)
    assert phase["stroke_end_count"] == 7  # Recorded frequency retains its source definition.


def test_participant_weights_are_equal_with_unequal_recording_counts():
    builds = [build(run=f"A{i}", participant="A", speed=1) for i in range(5)]
    builds += [build(run="B", participant="B", speed=2), build(run="C", participant="C", speed=3)]
    result = compare_phase_motion(build(), reference(builds))
    distribution = result["phases"][0]["metrics"]["speed_mm_s"]["reference"]
    assert distribution["available"] and distribution["recording_n"] == 7
    assert distribution["median"] == pytest.approx(2)
    assert sum(
        point["weight"] for point in distribution["points"] if point["participant_id"] == "A"
    ) == pytest.approx(1 / 3)


def test_self_exclusion_uses_whole_participant_and_original_recording_identity():
    builds = [build(run=f"E{i}", participant=f"P{i}", recording_id=f"T{i}") for i in range(4)]
    query = build(run="different-global-id", participant=None, recording_id="T0")
    result = compare_phase_motion(query, reference(builds))
    assert result["participant_id"] == "P0"
    assert result["reference_summary"]["excluded_recording_ids"] == ["E0"]
    assert result["phases"][0]["metrics"]["speed_mm_s"]["reference"]["available"]
    with pytest.raises(ValueError, match="conflicts"):
        compare_phase_motion(build(recording_id="T0", participant="wrong"), reference(builds))


def test_fewer_than_three_experts_keeps_measurement_but_abstains_from_comparison():
    result = compare_phase_motion(
        build(participant="P0"),
        reference([build(run=f"E{i}", participant=f"P{i}") for i in range(3)]),
    )
    metric = result["phases"][0]["metrics"]["speed_mm_s"]
    assert metric["available"]
    assert not metric["reference"]["available"]
    assert metric["reference"]["reason"] == "insufficient_expert_participants"


def test_saved_reference_loads_without_expert_source_files(tmp_path):
    payload = reference()
    path = tmp_path / "phase-reference.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    restored = load_phase_reference(path)
    assert compare_phase_motion(build(), restored)["phases"][0]["metrics"]["speed_mm_s"][
        "reference"
    ]["available"]
    altered = deepcopy(payload)
    altered["recordings"][0]["phases"][0]["metrics"]["speed_mm_s"]["value"] = 999
    with pytest.raises(ValueError, match="digest"):
        validate_phase_reference(altered)


def test_input_not_mutated_and_unknown_or_predrilling_phase_is_not_filled_in():
    rows = frames()
    rows[0].update(phase="pre_drilling", phase_id="pre")
    rows[-1].update(phase="unknown", phase_id="unknown")
    original = deepcopy(rows)
    result = build(rows)
    assert rows == original
    assert result["phases"][0]["duration_s"] == 6.5
    assert len(result["phases"]) == 1


@pytest.mark.parametrize("corruption", ["value", "coverage", "eligibility", "interval"])
def test_resealed_arithmetic_or_quality_corruption_is_rejected(corruption):
    payload = build()
    phase = payload["phases"][0]
    if corruption == "value":
        phase["metrics"]["speed_mm_s"]["value"] = 900
    elif corruption == "coverage":
        phase["coverage_fraction"] = 0.4
    elif corruption == "eligibility":
        phase["metrics"]["stroke_frequency_hz"]["eligible"] = False
    else:
        phase["intervals"][0]["native_end_s"] += 1
    payload["artifact_sha256"] = artifact_sha256(payload)
    with pytest.raises(ValueError):
        validate_phase_motion(payload)
