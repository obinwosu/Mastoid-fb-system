"""Whole-phase safety respects evidence gaps, interval identity and detector gates."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.phase_summary_safety import build_phase_safety


def phases():
    return [
        {"phase_name": "early_surface", "phase_id": "early-1", "t_start_s": 0, "t_end_s": 5},
        {"phase_name": "antrum", "phase_id": "antrum-1", "t_start_s": 5, "t_end_s": 10},
        {"phase_name": "early_surface", "phase_id": "early-2", "t_start_s": 10, "t_end_s": 12},
        {"phase_name": "incus", "phase_id": "incus-1", "t_start_s": 12, "t_end_s": 15},
    ]


def visibility(episodes=()):
    return {
        "available": True, "coaching_eligible": True,
        "detection_status": {"status": "completed"},
        "coaching_episodes": [
            {"t_start_s": start, "t_end_s": end, "coaching_eligible": True}
            for start, end in episodes
        ],
    }


def event(identity="event-1", start=1, end=2, **extra):
    return {
        "event_id": identity, "event_type": "simulator_overshoot", "anatomy": "FacialNerve",
        "t_start_s": start, "t_end_s": end,
        "attributes": {"coaching_eligible": True, "min_edt_mm": -9}, **extra,
    }


def test_visibility_clips_and_unions_overlaps_across_repeated_phases():
    # The early phase lasts [0,5) U [10,12); the gap must not become exposure.
    result = build_phase_safety(phases(), [], visibility([(1, 7), (3, 11), (14, 20)]))
    assert result["early_surface"]["drill_visibility"]["duration_s"] == 5
    assert result["antrum"]["drill_visibility"]["duration_s"] == 5
    assert result["incus"]["drill_visibility"]["duration_s"] == 1
    assert result["antrum"]["drill_visibility"]["continuous_occlusion_measured"] is False


@pytest.mark.parametrize("status", ["not_evaluable", "suppressed", "unavailable", "failed"])
def test_failed_visibility_is_missing_not_zero(status):
    ray = visibility()
    ray["detection_status"]["status"] = status
    metric = build_phase_safety(phases(), [], ray)["antrum"]["drill_visibility"]
    assert not metric["available"]
    assert metric["duration_s"] is None


def test_verified_empty_results_are_zero_but_missing_sources_are_not():
    result = build_phase_safety(phases(), [], visibility(), safety_available=True)
    assert result["antrum"]["critical_structure_violation"]["count"] == 0
    assert result["antrum"]["drill_visibility"]["duration_s"] == 0
    missing = build_phase_safety(phases(), [], {})["antrum"]
    assert missing["critical_structure_violation"]["count"] is None
    assert missing["drill_visibility"]["duration_s"] is None


def test_qualified_event_count_is_deduplicated_and_uses_phase_local_depth():
    cross_phase = event(start=4, end=7)
    ignored = event("small-boundary-signal", start=3, end=3,
                    attributes={"coaching_eligible": False})
    frames = [
        {"t_video_s": 4, "phase_id": "early-1", "voxel_min_FacialNerve_mm": -0.4},
        {"t_video_s": 6, "phase_id": "antrum-1", "voxel_min_FacialNerve_mm": -1.7},
        {"t_video_s": 13, "phase_id": "incus-1", "voxel_min_FacialNerve_mm": -8},
    ]
    result = build_phase_safety(phases(), [cross_phase, cross_phase, ignored], {},
                                frames=frames, safety_available=True)
    early = result["early_surface"]["critical_structure_violation"]
    antrum = result["antrum"]["critical_structure_violation"]
    assert early["count"] == 1
    assert early["raw_event_count"] == 2
    assert antrum["count"] == 1
    assert early["structures"][0]["max_intrusion_mm"] == 0.4
    assert antrum["structures"][0]["max_intrusion_mm"] == 1.7
    assert result["incus"]["critical_structure_violation"]["count"] == 0


def test_point_events_belong_to_matching_phase_and_final_endpoint_is_retained():
    rows = [event(start=5, end=5, phase_id="antrum-1"),
            event("final", start=15, end=15, phase_id="incus-1")]
    result = build_phase_safety(phases(), rows, {}, safety_available=True)
    assert result["early_surface"]["critical_structure_violation"]["count"] == 0
    assert result["antrum"]["critical_structure_violation"]["count"] == 1
    assert result["incus"]["critical_structure_violation"]["count"] == 1


def test_event_phase_identity_overrides_broad_time_overlap():
    row = event(start=4, end=7, phase_id="antrum-1")
    result = build_phase_safety(phases(), [row], {}, safety_available=True)
    assert result["early_surface"]["critical_structure_violation"]["count"] == 0
    assert result["antrum"]["critical_structure_violation"]["count"] == 1


def test_explicit_point_identity_preserves_a_recorded_interval_endpoint():
    row = event(start=5, end=5, phase_id="early-1")
    frames = [{"t_video_s": 5, "phase_id": "early-1", "voxel_min_FacialNerve_mm": -1.2}]
    result = build_phase_safety(phases(), [row], {}, frames=frames, safety_available=True)
    early = result["early_surface"]["critical_structure_violation"]
    assert early["count"] == 1
    assert early["structures"][0]["max_intrusion_mm"] == 1.2
    assert result["antrum"]["critical_structure_violation"]["count"] == 0


def test_only_qualified_segments_and_frames_contribute_depth():
    row = event(start=1, end=9, attributes={
        "coaching_eligible": True,
        "coaching_segments": [{"t_start_s": 6, "t_end_s": 7, "phase": "antrum"}],
        "min_edt_mm": -10,
    })
    frames = [
        {"t_video_s": 6, "phase": "antrum", "voxel_min_FacialNerve_mm": -0.3,
         "voxel_safety_signal_structures": "FacialNerve"},
        {"t_video_s": 6.5, "phase": "antrum", "voxel_min_FacialNerve_mm": -0.9,
         "voxel_safety_signal_structures": ""},
        {"t_video_s": 8, "phase": "antrum", "voxel_min_FacialNerve_mm": -8,
         "voxel_safety_signal_structures": "FacialNerve"},
    ]
    result = build_phase_safety(phases(), [row], {}, frames=frames, safety_available=True)
    assert result["early_surface"]["critical_structure_violation"]["count"] == 0
    assert result["antrum"]["critical_structure_violation"]["structures"][0][
        "max_intrusion_mm"
    ] == 0.3


def test_depth_remains_unknown_without_bound_local_frames():
    result = build_phase_safety(phases(), [event()], {}, safety_available=True)
    structure = result["early_surface"]["critical_structure_violation"]["structures"][0]
    assert structure["max_intrusion_mm"] is None


def test_absent_phase_and_grouped_input_have_explicit_missingness():
    grouped = [{"phase_name": "antrum", "intervals": [
        {"t_start_s": 1, "t_end_s": 4}, {"t_start_s": 8, "t_end_s": 9},
    ]}]
    result = build_phase_safety(grouped, [], visibility([(2, 8.5)]), safety_available=True)
    assert result["antrum"]["drill_visibility"]["duration_s"] == 2.5
    assert result["incus"]["drill_visibility"]["duration_s"] is None
    assert result["incus"]["critical_structure_violation"]["count"] is None


@pytest.mark.parametrize("video_scale", [1, 2])
def test_visibility_uses_native_seconds_for_supported_scaled_video_clock(video_scale):
    intervals = [{"phase_name": "antrum", "phase_id": "a",
                  "t_start_s": 0, "t_end_s": 6 * video_scale}]
    frames = [{"phase_id": "a", "t_video_s": time * video_scale, "t_abs": 100 + time}
              for time in range(7)]
    episodes = [(1 * video_scale, 4 * video_scale), (3 * video_scale, 5 * video_scale)]
    result = build_phase_safety(intervals, [], visibility(episodes), frames=frames)
    measure = result["antrum"]["drill_visibility"]
    assert measure["available"]
    assert measure["duration_s"] == pytest.approx(4)
    assert measure["duration_time_basis"] == "native_seconds"


def test_visibility_interpolates_piecewise_clock_without_global_scale_assumption():
    intervals = [{"phase_name": "antrum", "t_start_s": 0, "t_end_s": 8}]
    # Video runs twice native speed initially, then its times advance four times as fast.
    frames = [{"phase": "antrum", "t_video_s": video, "t_abs": 100 + native}
              for video, native in [(0, 0), (2, 1), (4, 2), (8, 3)]]
    result = build_phase_safety(intervals, [], visibility([(1, 6)]), frames=frames)
    assert result["antrum"]["drill_visibility"]["duration_s"] == pytest.approx(2)


@pytest.mark.parametrize("frames", [[], [
    {"phase": "antrum", "t_video_s": 2, "t_abs": 101},
    {"phase": "antrum", "t_video_s": 4, "t_abs": 102},
]])
def test_visibility_does_not_extrapolate_missing_native_clock(frames):
    intervals = [{"phase_name": "antrum", "t_start_s": 0, "t_end_s": 8}]
    result = build_phase_safety(intervals, [], visibility([(1, 6)]), frames=frames)
    measure = result["antrum"]["drill_visibility"]
    assert not measure["available"]
    assert measure["duration_s"] is None
