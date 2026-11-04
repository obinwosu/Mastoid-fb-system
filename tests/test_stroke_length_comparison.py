"""Completed source strokes retain physical length and phase-specific comparison."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.coaching_plan import build_session_coaching_plan
from evidence_system.motion_comparison import (
    LEGACY_PROTOCOL, add_stroke_length, artifact_sha256, build_expert_reference, build_motion_windows,
    compare_motion_windows, validate_window_build,
)


def rows(*, seconds=9, stroke_frames=4, scale=1., phase="antrum"):
    last = int(seconds * 4)
    return [
        {"t_abs": 1000. + i / 4, "t_video_s": 30. + i / 4,
         "drill_x": scale * i / 4000, "drill_y": 0., "drill_z": 0.,
         "phase": phase, "phase_id": f"phase:{phase}", "frame_removed_mm3": 0.,
         "is_drilling_frame": True, "stroke_id": (max(i, 1) - 1) // stroke_frames,
         "stroke_end_flag": (i > 0 and i % stroke_frames == 0) or i == last}
        for i in range(last + 1)
    ]


def base(source, *, run="query", participant="query"):
    return build_motion_windows(source, global_run_id=run, participant_id=participant,
        protocol=LEGACY_PROTOCOL,
        provenance={"pose_source": "canonical_vision_pose", "position_unit": "m",
            "phase_source_kind": "manual_frame_phase", "clock_source": "generated_native_clock",
            "source_artifacts": [
                {"kind": "frames", "path": "generated_frames.csv", "sha256": "a" * 64},
                {"kind": "phases", "path": "generated_phases.csv", "sha256": "b" * 64},
            ]})


def measured(source, **kwargs):
    return add_stroke_length(base(source, **kwargs), source)


def experts(*, phase="antrum", n=3, stroke_frames=8):
    return build_expert_reference([
        measured(rows(seconds=7, stroke_frames=stroke_frames, phase=phase),
                 run=f"expert:{i}", participant=f"expert:{i}") for i in range(n)
    ], expert_participant_ids=[f"expert:{i}" for i in range(n)],
       provenance={"expert_definition": "Generated source trajectories, not participant records"})


def long_stroke_rows():
    source = rows()
    for i, row in enumerate(source):
        row["stroke_id"] = 0 if i <= 24 else 1 if i <= 32 else 2
        row["stroke_end_flag"] = i in (24, 32, 36)
    return source


def test_whole_stroke_length_is_mm_and_is_not_clipped_to_window():
    source = long_stroke_rows()
    original = base(source)
    unchanged = deepcopy(original)
    enriched = add_stroke_length(original, source)
    assert original == unchanged
    first, second = enriched["windows"][:2]
    assert first["mean_stroke_length_mm"] is None
    assert first["stroke_length_count"] == 0
    assert not first["stroke_length_eligible"]
    assert second["native_start_s"] == 1 and second["native_end_s"] == 6
    assert second["stroke_length_ids"] == [0]
    assert second["mean_stroke_length_mm"] == pytest.approx(6.)
    assert second["stroke_length_count"] == 1
    assert second["stroke_length_eligible"]
    for before, after in zip(original["windows"], enriched["windows"], strict=True):
        assert {key: after[key] for key in before} == before
    validate_window_build(enriched)


def test_forced_last_endpoint_keeps_raw_path_but_cannot_supply_length():
    enriched = measured(long_stroke_rows())
    final = enriched["stroke_lengths"][-1]
    assert final["length_mm"] == pytest.approx(.75)
    assert not final["eligible"]
    assert "forced_terminal_boundary" in final["exclusion_reasons"]
    last_window = enriched["windows"][-1]
    assert last_window["stroke_length_candidate_count"] == 3
    assert last_window["stroke_length_count"] == 2
    assert last_window["mean_stroke_length_mm"] == pytest.approx((6 + 1.75) / 2)


def test_gap_excludes_whole_stroke_even_when_its_endpoint_is_supported():
    source = long_stroke_rows()
    del source[8]
    enriched = measured(source)
    stroke = enriched["stroke_lengths"][0]
    assert stroke["max_gap_s"] == .5
    assert stroke["length_mm"] == pytest.approx(6.)
    assert not stroke["eligible"] and "unsupported_gap" in stroke["exclusion_reasons"]
    window = enriched["windows"][1]
    assert window["stroke_length_candidate_count"] == 1
    assert window["mean_stroke_length_mm"] is None
    assert not window["stroke_length_eligible"]


def test_cross_phase_stroke_is_excluded_instead_of_assigned_by_endpoint():
    source = long_stroke_rows()
    for row in source[12:]:
        row.update(phase="incus", phase_id="phase:incus")
    enriched = measured(source)
    stroke = enriched["stroke_lengths"][0]
    assert not stroke["eligible"] and "phase_boundary" in stroke["exclusion_reasons"]
    assert all(0 not in window["stroke_length_ids"] for window in enriched["windows"])
    assert enriched["windows"][0]["mean_stroke_length_mm"] == pytest.approx(1.75)


def test_nonfinite_legacy_source_and_single_point_strokes_are_not_zero_length_evidence():
    source = rows()
    for row in source:
        row["drill_x"] = 0.
    enriched = measured(source)
    assert all(not stroke["eligible"] for stroke in enriched["stroke_lengths"])
    assert all(window["mean_stroke_length_mm"] is None for window in enriched["windows"])
    assert "source_segmentation_invalid" in enriched["stroke_lengths"][0]["exclusion_reasons"]
    source = rows()
    source[12].update(stroke_id=99, stroke_end_flag=True)
    enriched = measured(source)
    singleton = next(stroke for stroke in enriched["stroke_lengths"] if stroke["stroke_id"] == 99)
    assert not singleton["eligible"] and "insufficient_points" in singleton["exclusion_reasons"]


@pytest.mark.parametrize("field,value", [
    ("mean_stroke_length_mm", 123.), ("stroke_length_count", 999),
    ("stroke_length_ids", [999]), ("stroke_length_eligible", False),
])
def test_resealed_window_length_values_must_agree_with_source_stroke_table(field, value):
    enriched = measured(rows())
    enriched["windows"][0][field] = value
    enriched["artifact_sha256"] = artifact_sha256(enriched)
    with pytest.raises(ValueError):
        validate_window_build(enriched)


@pytest.mark.parametrize("phase", ["early_surface", "antrum", "incus"])
def test_shorter_length_compares_by_phase_and_produces_separate_coaching(phase):
    query = measured(rows(seconds=7, stroke_frames=4, phase=phase))
    comparison = compare_motion_windows(query, experts(phase=phase))
    assert comparison["episodes"] == []  # Physical speed is identical in both groups.
    assert len(comparison["length_episodes"]) == 1
    episode = comparison["length_episodes"][0]
    assert episode["window_count"] == 3
    assert episode["native_end_s"] - episode["native_start_s"] == 7
    for window in comparison["windows"]:
        length = window["comparisons"]["mean_stroke_length_mm"]
        assert length["status"] == "compared"
        assert length["percentile"] == 0
        assert length["observed"] < length["reference_q25"]
        assert window["candidate_short"] and window["persistent_short"]
        assert not window["candidate_low"]
    plan = build_session_coaching_plan([], [], {}, None, motion_comparison=comparison)
    assert len(plan["items"]) == 1
    assert plan["items"][0]["feedback_id"] == "coaching:short_strokes"
    assert plan["items"][0]["review_clips"]
    assert all(clip["phase"] == ("early_phase" if phase == "early_surface" else phase)
               for clip in plan["items"][0]["review_clips"])
    assert plan["items"][0]["category"] == "technique"


def test_length_needs_three_experts_and_persistence_and_ties_are_not_short():
    query = measured(rows(seconds=7, stroke_frames=4))
    assert compare_motion_windows(query, experts(n=2))["length_episodes"] == []
    assert compare_motion_windows(measured(rows(seconds=6)), experts())["length_episodes"] == []
    tied = compare_motion_windows(measured(rows(seconds=7, stroke_frames=8)), experts())
    assert tied["length_episodes"] == []
    assert all(not window["candidate_short"] for window in tied["windows"])
    near_tied = compare_motion_windows(
        measured(rows(seconds=7, stroke_frames=8, scale=1. - 1e-11)), experts())
    assert all(not window["candidate_short"] for window in near_tied["windows"])


def test_one_short_stroke_repeated_in_three_overlapping_windows_cannot_trigger_coaching():
    source = rows(seconds=7)
    for i, row in enumerate(source):
        row["stroke_id"] = 0 if i <= 12 else 1
        row["stroke_end_flag"] = i in (12, 28)
    comparison = compare_motion_windows(measured(source), experts(stroke_frames=16))
    assert len(comparison["windows"]) == 3
    assert all(window["candidate_short"] for window in comparison["windows"])
    assert all(window["stroke_length_ids"] == [0] for window in comparison["windows"])
    assert comparison["length_episodes"] == []
    assert all(not window["persistent_short"] for window in comparison["windows"])
    assert build_session_coaching_plan([], [], {}, None, motion_comparison=comparison)["items"] == []
