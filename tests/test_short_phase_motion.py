"""Short observed phases remain samples with their actual native duration."""

from copy import deepcopy

import pytest
from test_motion_comparison import frames
from test_nonoverlap_motion import current
from test_stroke_length_comparison import rows as stroke_rows

from evidence_system.cli import build_motion_reference
from evidence_system.motion_comparison import (
    DEFAULT_PROTOCOL,
    TEN_SECOND_PROTOCOL,
    TEN_SECOND_SHORT_PHASE_PROTOCOL,
    add_stroke_frequency,
    add_stroke_length,
    artifact_sha256,
    build_expert_reference,
    compare_motion_windows,
    summarize_expert_reference,
    validate_window_build,
)


def short_phase(source):
    return current(source, protocol=TEN_SECOND_SHORT_PHASE_PROTOCOL)


def test_whole_short_phase_uses_actual_native_duration_for_rates_and_coverage():
    source = frames(seconds=7, video_scale=2)
    result = short_phase(source)
    assert len(result["windows"]) == 1
    window = result["windows"][0]
    assert (window["native_start_s"], window["native_end_s"]) == (0, 7)
    assert (window["t_start_s"], window["t_end_s"]) == (0, 14)
    assert window["window_duration_s"] == 7
    assert window["window_kind"] == "whole_short_phase"
    assert window["coverage_fraction"] == window["removal_coverage_fraction"] == 1
    assert window["speed_mm_s"] == pytest.approx(1)
    assert window["removal_rate_mm3_s"] == pytest.approx(1)
    validate_window_build(result)
    assert current(source, protocol=TEN_SECOND_PROTOCOL)["windows"] == []
    assert current(source)["protocol"] == DEFAULT_PROTOCOL


def test_short_phase_frequency_uses_seven_seconds_and_preserves_complete_strokes():
    source = stroke_rows(seconds=7)
    base = short_phase(source)
    measured = add_stroke_length(add_stroke_frequency(base, source), source)
    window = measured["windows"][0]
    assert window["stroke_end_count"] == 7
    assert window["stroke_frequency_hz"] == 1
    assert window["stroke_drilling_time_s"] == 7
    assert window["stroke_rate_per_s"] == 1
    assert window["stroke_length_ids"] == list(range(6))
    assert (
        measured["stroke_lengths"] == add_stroke_length(current(source), source)["stroke_lengths"]
    )
    validate_window_build(measured)


def test_phase_fallback_does_not_cross_boundaries_or_invent_a_long_phase_tail():
    source = frames(seconds=6, phase="early_surface")
    source += frames(seconds=12, phase="antrum", origin=6.25, video_offset=6.25)
    result = short_phase(source)
    assert [(w["native_start_s"], w["native_end_s"]) for w in result["windows"]] == [
        (0, 6),
        (6.25, 16.25),
    ]
    assert [w["window_kind"] for w in result["windows"]] == ["whole_short_phase", "full_window"]
    validate_window_build(result)
    ten = short_phase(frames(seconds=10))["windows"]
    assert len(ten) == 1 and ten[0]["window_kind"] == "full_window"
    zero = short_phase(frames(seconds=0))
    assert zero["windows"] == []
    validate_window_build(zero)


def test_short_phase_keeps_real_gaps_and_missing_measurements():
    source = [row for row in frames(seconds=7) if not 1 < row["t_abs"] < 2]
    window = short_phase(source)["windows"][0]
    assert window["coverage_fraction"] == pytest.approx(6 / 7)
    assert window["speed_mm_s"] == pytest.approx(1)
    assert window["removal_rate_mm3_s"] == pytest.approx(6 / 7)
    for row in source:
        row["drill_x"] = None
    missing = short_phase(source)["windows"][0]
    assert missing["window_kind"] == "whole_short_phase"
    assert missing["speed_mm_s"] is None and not missing["eligible"]


@pytest.mark.parametrize(
    "change",
    [
        {"window_duration_s": 10},
        {"native_start_s": 1},
        {"removal_rate_mm3_s": 0.7},
        {"coverage_fraction": 0.7},
        {"window_kind": "full_window"},
    ],
)
def test_short_phase_contract_rejects_truncation_or_nominal_denominators(change):
    result = deepcopy(short_phase(frames(seconds=7)))
    result["windows"][0].update(change)
    result["artifact_sha256"] = artifact_sha256(result)
    with pytest.raises(ValueError):
        validate_window_build(result)


def test_short_phase_summary_is_included_but_does_not_activate_an_event_policy():
    build = short_phase(frames(seconds=7))
    reference = build_expert_reference(
        [build],
        expert_participant_ids=[build["participant_id"]],
        provenance={"expert_definition": "Synthetic reference for duration checks"},
    )
    summary = summarize_expert_reference(reference)
    assert summary["window_layout"]["whole_short_phase_n"] == 1
    assert summary["window_layout"]["actual_duration_min_s"] == 7
    distribution = summary["metrics"]["speed_mm_s"]["phases"]["antrum"]
    assert distribution["window_n"] == 1
    assert distribution["median"] == pytest.approx(1)
    with pytest.raises(ValueError, match="offline distributions"):
        compare_motion_windows(build, reference)


def test_short_phase_cli_option_cannot_change_default_five_second_grid(tmp_path):
    with pytest.raises(ValueError, match="only for the offline 10s"):
        build_motion_reference(
            tmp_path / "unused", tmp_path / "new.json", include_short_phases=True
        )
