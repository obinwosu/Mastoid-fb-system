"""Recorded stroke counts keep distinct window and drilling-time denominators."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.motion_comparison import (
    add_stroke_frequency, artifact_sha256, build_motion_windows, validate_window_build,
)


def source_rows(*, origin=1000., video_scale=1.):
    # Seven source strokes end at integer native seconds. A left-boundary row
    # belongs to the preceding stroke, distinguishing overlap from end counts.
    return [
        {"t_abs": origin + i / 4, "t_video_s": 100 + video_scale * i / 4,
         "drill_x": i / 2000, "drill_y": 0., "drill_z": 0.,
         "phase": "antrum", "phase_id": "phase:antrum",
         "frame_removed_mm3": 0., "is_drilling_frame": True,
         "stroke_id": (max(i, 1) - 1) // 4,
         "stroke_end_flag": i > 0 and i % 4 == 0}
        for i in range(29)
    ]


def windows(rows):
    return build_motion_windows(rows, global_run_id="generated:reference",
        participant_id="generated_expert", provenance={
            "pose_source": "canonical_vision_pose", "position_unit": "m",
            "phase_source_kind": "manual_frame_phase", "clock_source": "generated_native_clock",
            "source_artifacts": [
                {"kind": "frames", "path": "generated_frames.csv", "sha256": "a" * 64},
                {"kind": "phases", "path": "generated_phases.csv", "sha256": "b" * 64},
            ],
        })


def test_endpoint_counts_and_overlapping_ids_have_distinct_numerators():
    rows = source_rows()
    for row in rows:
        row["is_drilling_frame"] = "True"
        row["stroke_end_flag"] = str(row["stroke_end_flag"])
    enriched = add_stroke_frequency(windows(rows), rows)
    window = enriched["windows"][1]  # Native [1,6], end events on (1,6].
    assert window["stroke_end_count"] == 5
    assert window["stroke_frequency_hz"] == 1
    assert window["stroke_count"] == 6
    assert window["stroke_drilling_time_s"] == 5
    assert window["stroke_rate_per_s"] == pytest.approx(1.2)
    assert window["stroke_frequency_eligible"] and window["stroke_rate_eligible"]
    validate_window_build(enriched)


def test_native_denominators_and_old_measurements_are_preserved_without_mutation():
    rows = source_rows(video_scale=2.)
    original = windows(rows)
    untouched = deepcopy(original)
    enriched = add_stroke_frequency(original, rows)
    assert original == untouched
    for old, new in zip(original["windows"], enriched["windows"], strict=True):
        assert {key: new[key] for key in old} == old
    for key in ("input_rows_sha256", "protocol", "protocol_sha256", "measurement_contract", "provenance"):
        assert enriched[key] == original[key]
    first = enriched["windows"][0]
    assert first["t_end_s"] - first["t_start_s"] == 10
    assert first["stroke_drilling_time_s"] == 5
    assert first["stroke_rate_per_s"] == 1
    assert first["stroke_frequency_hz"] == 1
    assert enriched["artifact_sha256"] == artifact_sha256(enriched)
    validate_window_build(original)  # Existing references still validate.


def test_drilling_time_uses_right_hand_mask_and_one_second_interval_cap():
    rows = source_rows()
    rows[12].update(is_drilling_frame=False, stroke_id=None, stroke_end_flag=False)
    # A 1.5-second observed gap is counted as 1 second by the historical
    # drilling-time formula. It remains a gap for the existing coverage rule.
    rows = [row for i, row in enumerate(rows) if not 5 <= i <= 9]
    enriched = add_stroke_frequency(windows(rows), rows)
    first = enriched["windows"][0]
    assert first["stroke_drilling_time_s"] == pytest.approx(4.25)
    assert first["stroke_rate_per_s"] == pytest.approx(first["stroke_count"] / 4.25)
    assert not first["eligible"]
    assert not first["stroke_frequency_eligible"] and not first["stroke_rate_eligible"]


def test_no_drilling_time_is_missing_rate_not_zero_frequency():
    rows = source_rows()
    for i, row in enumerate(rows):
        row["is_drilling_frame"] = i > 20
        row["stroke_id"] = 0 if i > 20 else None
        row["stroke_end_flag"] = i == 28
    enriched = add_stroke_frequency(windows(rows), rows)
    first = enriched["windows"][0]
    assert first["stroke_count"] == 0 and first["stroke_end_count"] == 0
    assert first["stroke_frequency_hz"] == 0
    assert first["stroke_drilling_time_s"] == 0
    assert first["stroke_rate_per_s"] is None
    assert not first["stroke_rate_eligible"]
    validate_window_build(enriched)


def test_nonfinite_legacy_turns_retain_counts_but_are_not_reference_eligible():
    rows = source_rows()
    for row in rows:
        row.update(drill_x=0., stroke_id=0, stroke_end_flag=False)
    rows[-1]["stroke_end_flag"] = True
    enriched = add_stroke_frequency(windows(rows), rows)
    metadata = enriched["stroke_frequency_metadata"]
    assert metadata["source_status"] == "nonfinite_legacy_turn_angles"
    assert metadata["nonfinite_turn_angle_count"] > 0
    assert enriched["windows"][-1]["stroke_end_count"] == 1
    assert enriched["windows"][-1]["stroke_frequency_hz"] == .2
    assert all(not row["stroke_frequency_eligible"] and not row["stroke_rate_eligible"]
               for row in enriched["windows"])
    validate_window_build(enriched)


@pytest.mark.parametrize("field,value", [
    ("stroke_frequency_hz", 99.), ("stroke_rate_per_s", 99.),
    ("stroke_end_count", -1), ("stroke_drilling_time_s", 6.),
])
def test_resigned_arithmetic_tampering_is_rejected(field, value):
    rows = source_rows()
    enriched = add_stroke_frequency(windows(rows), rows)
    enriched["windows"][0][field] = value
    enriched["artifact_sha256"] = artifact_sha256(enriched)
    with pytest.raises(ValueError):
        validate_window_build(enriched)


@pytest.mark.parametrize("field,value", [
    ("stroke_end_flag", None), ("is_drilling_frame", "unknown"), ("stroke_id", None),
])
def test_missing_or_ambiguous_source_values_are_not_invented(field, value):
    rows = source_rows()
    original = windows(rows)
    rows[8][field] = value
    with pytest.raises(ValueError):
        add_stroke_frequency(original, rows)
