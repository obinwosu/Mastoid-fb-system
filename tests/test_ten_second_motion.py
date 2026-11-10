"""Offline ten-second aggregation preserves native clocks and complete strokes."""

import pytest
from test_motion_comparison import frames
from test_nonoverlap_motion import current, experts
from test_stroke_length_comparison import rows as stroke_rows

from evidence_system.motion_comparison import (
    DEFAULT_PROTOCOL,
    TEN_SECOND_PROTOCOL,
    add_stroke_frequency,
    add_stroke_length,
    compare_motion_windows,
    summarize_expert_reference,
    validate_window_build,
)


def test_ten_second_grid_keeps_native_clock_phase_boundaries_and_full_windows():
    source = frames(seconds=23, video_scale=2)
    result = current(source, protocol=TEN_SECOND_PROTOCOL)
    assert [(w["native_start_s"], w["native_end_s"]) for w in result["windows"]] == [
        (0, 10),
        (10, 20),
    ]
    assert [(w["t_start_s"], w["t_end_s"]) for w in result["windows"]] == [(0, 20), (20, 40)]
    assert all(w["speed_mm_s"] == pytest.approx(1) for w in result["windows"])
    assert all(w["removal_rate_mm3_s"] == pytest.approx(1) for w in result["windows"])
    for row in source:
        if row["t_abs"] >= 12:
            row.update(phase="incus", phase_id="incus")
    result = current(source, protocol=TEN_SECOND_PROTOCOL)
    assert [(w["native_start_s"], w["native_end_s"]) for w in result["windows"]] == [
        (0, 10),
        (12, 22),
    ]
    assert DEFAULT_PROTOCOL["window_s"] == DEFAULT_PROTOCOL["stride_s"] == 5
    assert current(source)["protocol"] == DEFAULT_PROTOCOL


def test_ten_second_length_reaggregates_complete_strokes_not_five_second_means():
    source = stroke_rows(seconds=12)
    for i, row in enumerate(source):
        row.update(
            drill_x=i / 4096,
            stroke_id=0 if i <= 8 else 1 if i <= 16 else 2 if i <= 40 else 3,
            stroke_end_flag=i in (8, 16, 40, 48),
        )
    five = add_stroke_length(current(source), source)
    ten = add_stroke_length(current(source, protocol=TEN_SECOND_PROTOCOL), source)
    assert ten["stroke_lengths"] == five["stroke_lengths"]
    first = ten["windows"][0]
    assert first["stroke_length_ids"] == [0, 1, 2]
    assert first["mean_stroke_length_mm"] == pytest.approx((8 + 7 + 23) * 1000 / 4096 / 3)
    naive_mean = sum(w["mean_stroke_length_mm"] for w in five["windows"]) / 2
    assert first["mean_stroke_length_mm"] != pytest.approx(naive_mean)
    enriched = add_stroke_frequency(ten, source)
    assert enriched["windows"][0]["stroke_frequency_hz"] == pytest.approx(3 / 10)
    frequency_contract = enriched["stroke_frequency_metadata"]["contract"]
    assert "(10 seconds)" in frequency_contract["stroke_frequency_hz"]
    validate_window_build(enriched)


def test_ten_second_speed_uses_observed_duration_and_preserves_gaps():
    source = frames(seconds=10)
    for row in source:
        t = row["t_abs"]
        row["drill_x"] = (min(t, 5) + max(t - 5, 0) * 3) / 1000
    source = [row for row in source if not 1 < row["t_abs"] < 2]
    result = current(source, protocol=TEN_SECOND_PROTOCOL)["windows"][0]
    assert result["coverage_fraction"] == pytest.approx(0.9)
    assert result["speed_mm_s"] == pytest.approx((4 * 1 + 5 * 3) / 9)
    assert result["removal_rate_mm3_s"] == pytest.approx(9 / 10)


def test_ten_second_reference_cannot_silently_replace_a_five_second_comparison():
    reference = experts(protocol=TEN_SECOND_PROTOCOL)
    with pytest.raises(ValueError, match="protocols"):
        compare_motion_windows(current(frames(seconds=16)), reference)
    summary = summarize_expert_reference(reference)
    distribution = summary["metrics"]["speed_mm_s"]["phases"]["antrum"]
    assert summary["window_count"] == 3
    assert distribution["participant_n"] == distribution["recording_n"] == 3
    assert distribution["total_weight"] == pytest.approx(1)
    assert distribution["median"] == pytest.approx(10)
    assert summary["metrics"]["speed_mm_s"]["phases"]["incus"]["median"] is None
