"""Selection policy tests use generated measurements, never clinical labels."""

import json
import sys
from bisect import bisect_left, bisect_right
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.event_review import _ranking_scale, build_event_review
from evidence_system.motion_comparison import DEFAULT_PROTOCOL, _distribution, artifact_sha256


def inputs(values, *, phase="antrum", metric="speed_mm_s", self_exclude=False):
    records = []
    for i, speed in enumerate((8, 10, 12)):
        records.append(
            {
                "global_run_id": f"expert{i}",
                "participant_id": f"expert{i}",
                "windows": [
                    {
                        "phase": phase,
                        "speed_mm_s": speed,
                        "eligible": True,
                        "mean_stroke_length_mm": 2 + i * 2,
                        "stroke_length_eligible": True,
                        "removal_rate_mm3_s": 1 + i,
                        "removal_eligible": True,
                    }
                ],
            }
        )
    if self_exclude:
        records.append(
            {
                "global_run_id": "other_current_run",
                "participant_id": "current",
                "windows": [{**records[0]["windows"][0], "speed_mm_s": 1000}],
            }
        )
    reference = {
        "schema": "motion_expert_reference_v1",
        "protocol": deepcopy(DEFAULT_PROTOCOL),
        "recordings": records,
    }
    reference["artifact_sha256"] = artifact_sha256(reference)
    included = records[:3]
    metrics = ("speed_mm_s", "mean_stroke_length_mm", "removal_rate_mm3_s")
    distributions = {m: _distribution(included, phase, m) for m in metrics}
    motion = {
        "available": True,
        "global_run_id": "case",
        "participant_id": "current",
        "protocol": deepcopy(DEFAULT_PROTOCOL),
        "reference_sha256": reference["artifact_sha256"],
        "reference_summary": {
            "excluded_recording_ids": ["other_current_run"] if self_exclude else []
        },
        "per_phase": {
            phase: {
                m: {k: v for k, v in d.items() if k not in ("values", "cumulative", "total_weight")}
                for m, d in distributions.items()
            }
        },
        "windows": [],
    }
    for i, value in enumerate(values):
        d = distributions[metric]
        lo, hi = bisect_left(d["values"], value), bisect_right(d["values"], value)
        percentile = 100 * (d["cumulative"][lo] + d["cumulative"][hi]) / 2 / d["total_weight"]
        motion["windows"].append(
            {
                "window_id": f"window:{i}",
                "phase": phase,
                "phase_id": f"phase:{phase}",
                "native_start_s": i * 5,
                "native_end_s": i * 5 + 5,
                # Different clock scaling proves selection preserves display and native bounds.
                "t_start_s": i * 4,
                "t_end_s": i * 4 + 4,
                "display_clock_valid": True,
                "user_control_fraction": 0,
                "comparisons": {
                    metric: {
                        "status": "compared",
                        "observed": value,
                        "percentile": percentile,
                        "reference_median": d["median"],
                        "relative_deficit": (d["median"] - value) / d["median"],
                    }
                },
            }
        )
    return motion, reference


def review(motion, reference, **kwargs):
    return build_event_review([], [], {}, motion, reference, duration_s=500, **kwargs)


def item(payload, metric="speed_mm_s"):
    return next(i for i in payload["items"] if i["metric_key"] == metric)


def test_bidirectional_rank_top_four_and_native_display_clocks():
    motion, reference = inputs([1, 50, 2, 40, 3, 15, 4, 14, 5, 13, 6])
    original = deepcopy((motion, reference))
    result = item(review(motion, reference))
    assert [c["observed"] for c in result["clips"]] == [50, 40, 1, 2]
    assert [c["rank"] for c in result["clips"]] == [1, 2, 3, 4]
    assert {c["direction"] for c in result["clips"]} == {"high", "low"}
    assert result["candidate_count"] == 11
    assert result["clips"][0]["t_start_s"] == 4
    assert result["clips"][0]["native_start_s"] == 5
    assert result["clips"][0]["playback_start_s"] == 0
    assert result["clips"][0]["playback_end_s"] == 13
    assert not any(c["keyframe_available"] for c in result["clips"])
    assert (motion, reference) == original


def test_same_direction_contiguous_candidates_collapse_to_strongest_window():
    motion, reference = inputs([1, 2, 30, 40, 10, 0, 1])
    result = item(review(motion, reference))
    assert [c["observed"] for c in result["clips"]] == [40, 0, 1]
    assert result["candidate_count"] == 6 and result["episode_count"] == 3
    assert result["clips"][0]["source_ids"] == ["window:2", "window:3"]
    assert result["clips"][0]["episode_window_count"] == 2
    assert result["clips"][0]["t_start_s"] == 12
    assert result["clips"][0]["t_end_s"] == 16
    assert result["clips"][0]["episode_start_s"] == 8


@pytest.mark.parametrize(
    ("metric", "values", "expected"),
    [
        ("mean_stroke_length_mm", [1, 2, 4, 6, 9], [9, 1]),
        ("removal_rate_mm3_s", [0, 1, 2, 3, 5], [5, 0]),
    ],
)
def test_length_and_removal_two_sided_strict_iqr_policy(metric, values, expected):
    motion, reference = inputs(values, metric=metric)
    result = item(review(motion, reference), metric)
    assert [c["observed"] for c in result["clips"]] == expected
    assert result["reference"] is None
    assert result["clips"][0]["reference"]["comparison_basis"] == "same_phase_5s_windows"


def test_missing_comparison_clock_and_control_guards_break_episodes():
    motion, reference = inputs([1, 1, 1, 1, 1])
    motion["windows"][1]["display_clock_valid"] = False
    motion["windows"][2]["comparisons"]["speed_mm_s"]["status"] = "not_evaluable"
    motion["windows"][3]["user_control_fraction"] = 0.2
    result = item(review(motion, reference))
    assert [c["window_id"] for c in result["clips"]] == ["window:0", "window:4"]
    assert result["eligible_window_count"] == 2
    motion["windows"][0]["comparisons"]["speed_mm_s"]["observed"] = None
    assert item(review(motion, reference))["candidate_count"] == 1


def test_phase_alias_and_self_exclusion_preserve_weighted_reference():
    motion, reference = inputs([1, 40], phase="early_phase", self_exclude=True)
    result = item(review(motion, reference))
    ref = result["clips"][0]["reference"]
    assert ref["median"] == 10
    assert ref["participant_n"] == 3
    assert ref["window_n"] == 3
    assert len(ref["points"]) == 101
    assert max(ref["points"]) == 12
    assert {c["phase"] for c in result["clips"]} == {"early_surface"}
    assert result["phase_status"]["incus"]["selected_count"] == 0
    motion["reference_summary"]["excluded_recording_ids"] = []
    assert item(review(motion, reference))["reason"] == "motion_reference_self_exclusion_mismatch"


@pytest.mark.parametrize("mutation", ["hash", "protocol", "distribution", "missing"])
def test_mismatched_or_missing_reference_fails_closed(mutation):
    motion, reference = inputs([0])
    if mutation == "hash":
        reference["recordings"][0]["windows"][0]["speed_mm_s"] = 99
    elif mutation == "protocol":
        motion["protocol"]["stride_s"] = 1
    elif mutation == "distribution":
        motion["per_phase"]["antrum"]["speed_mm_s"]["median"] = 99
    else:
        reference = None
    result = item(review(motion, reference))
    assert result["status"] == "unavailable"
    assert result["reference"] is None and result["clips"] == []


def test_safety_qualified_events_rank_by_depth_duration_and_verified_anchor():
    phases = [
        {"phase_name": "early_surface", "t_start_s": 0, "t_end_s": 10},
        {"phase_name": "antrum", "t_start_s": 10, "t_end_s": 30},
    ]
    events = [
        {
            "event_id": f"event:{i}",
            "event_type": "simulator_overshoot",
            "t_start_s": 10 + i,
            "t_end_s": 10.5 + i,
            "attributes": {"coaching_eligible": i != 6, "min_edt_mm": -depth},
        }
        for i, depth in enumerate((1, 3, 2, 6, 4, 5, 100))
    ]
    visibility = {
        "available": True,
        "coaching_episodes": [
            {
                "episode_id": "crossing",
                "t_start_s": 8,
                "t_end_s": 20,
                "anchor_t_s": 9,
                "anchor_frame_idx_global": 9,
                "strength_score": 0,
            },
            {"episode_id": "short", "t_start_s": 1, "t_end_s": 3, "strength_score": 999},
        ],
    }

    def enrich(plan):
        for wrapper in plan["items"]:
            for clip in wrapper["review_clips"]:
                if "anchor_t_s" in clip:
                    clip.update(
                        keyframe_available=True,
                        keyframe_time_s=clip["anchor_t_s"],
                        keyframe_frame_idx_global=9,
                        keyframe_url="/verified",
                    )

    result = build_event_review(
        events, phases, visibility, None, None, duration_s=30, enrich_keyframes=enrich
    )
    violation = item(result, "critical_structure_violation")
    assert [c["observed"] for c in violation["clips"]] == [6, 5, 4, 3]
    assert violation["candidate_count"] == 6 and violation["reference"] is None
    poor_view = item(result, "poor_drill_visibility")
    assert [c["observed"] for c in poor_view["clips"]] == [12, 2]
    assert poor_view["clips"][0]["keyframe_url"] == "/verified"
    assert "signal_time_s" not in violation["clips"][0]
    fallback = build_event_review(events, phases, visibility, None, None, duration_s=30)
    assert item(fallback, "poor_drill_visibility")["clips"][0]["observed"] == 12
    assert not item(fallback, "poor_drill_visibility")["clips"][0]["keyframe_available"]


def test_speed_quartiles_replace_legacy_percentile_and_relative_difference_gate():
    # Both deviations are less than 25% of the median, but outside Q25/Q75.
    motion, reference = inputs([7.9, 12.1])
    result = review(motion, reference)
    assert sorted(c["observed"] for c in item(result)["clips"]) == [7.9, 12.1]
    assert result["policy"]["relative_difference_threshold"] is None
    assert motion["protocol"]["min_relative_deficit"] == 0.25


def test_removal_comparison_remains_evaluable_when_legacy_speed_available_is_false():
    motion, reference = inputs([0], metric="removal_rate_mm3_s")
    motion.update(available=False, reason="no_eligible_windows_with_sufficient_reference")
    result = review(motion, reference)
    assert item(result, "removal_rate_mm3_s")["status"] == "ready"
    assert item(result, "speed_mm_s")["status"] == "unavailable"


def phase_inputs(config, *, factor=1.0):
    """Build matched multi-phase comparisons with independently chosen phase spreads."""
    motion, reference = inputs([])
    motion["per_phase"] = {}
    for record in reference["recordings"]:
        record["windows"] = []
    for phase_index, (phase, (current, experts)) in enumerate(config.items()):
        source, source_ref = inputs(current, phase=phase)
        for i, record in enumerate(reference["recordings"]):
            for window in source_ref["recordings"][i]["windows"]:
                window["speed_mm_s"] = experts[i] * factor
                record["windows"].append(window)
        dist = _distribution(reference["recordings"], phase, "speed_mm_s")
        motion["per_phase"][phase] = {
            metric: {
                k: v
                for k, v in _distribution(reference["recordings"], phase, metric).items()
                if k not in ("values", "cumulative", "total_weight")
            }
            for metric in ("speed_mm_s", "mean_stroke_length_mm", "removal_rate_mm3_s")
        }
        for window in source["windows"]:
            window["window_id"] = f"{phase}:{window['window_id']}"
            for key in ("native_start_s", "native_end_s", "t_start_s", "t_end_s"):
                window[key] += phase_index * 100
            window["comparisons"]["speed_mm_s"].update(
                observed=window["comparisons"]["speed_mm_s"]["observed"] * factor,
                reference_median=dist["median"],
                relative_deficit=None,
                percentile=None,
            )
            motion["windows"].append(window)
    reference["artifact_sha256"] = artifact_sha256(reference)
    motion["reference_sha256"] = reference["artifact_sha256"]
    return motion, reference


def test_global_cap_uses_phase_normalized_rank_and_clip_bound_references():
    motion, reference = phase_inputs(
        {
            "early_phase": ([160, 100, 140], [80, 100, 120]),
            "antrum": ([26, 10, 22, 10, 18], [8, 10, 12]),
            "incus": ([2], [0.8, 1, 1.2]),
        }
    )
    payload = review(motion, reference)
    selected = item(payload)
    assert payload["schema"] == "session_event_review_v2"
    assert payload["policy"]["top_n"] == 4 and payload["policy"]["scope"] == "session"
    assert len(payload["items"]) == 5
    assert all(row["phase"] == "session" for row in payload["items"])
    assert all("items" not in phase for phase in payload["phases"])
    assert [clip["observed"] for clip in selected["clips"]] == [26, 22, 2, 18]
    assert [clip["rank"] for clip in selected["clips"]] == [1, 2, 3, 4]
    assert [clip["reference"]["median"] for clip in selected["clips"]] == [10, 10, 1, 10]
    assert selected["phase_status"]["early_surface"]["candidate_count"] == 2
    assert selected["phase_status"]["early_surface"]["selected_count"] == 0
    assert selected["phase_status"]["early_surface"]["status"] == "candidates_available"
    assert selected["phase_status"]["incus"]["selected_count"] == 1
    assert sum(len(row["clips"]) for row in payload["items"]) <= 20


@pytest.mark.parametrize("metric", ["speed_mm_s", "mean_stroke_length_mm", "removal_rate_mm3_s"])
def test_all_motion_metrics_use_strict_quartiles_and_numerical_ties(metric):
    motion, reference = inputs([], metric=metric)
    distribution = motion["per_phase"]["antrum"][metric]
    lo, hi = distribution["q25"], distribution["q75"]
    motion, reference = inputs(
        [lo, lo - 1e-10, lo - 0.01, hi, hi + 1e-10, hi + 0.01], metric=metric
    )
    result = item(review(motion, reference), metric)
    assert {clip["window_id"] for clip in result["clips"]} == {"window:2", "window:5"}
    assert {clip["direction"] for clip in result["clips"]} == {"low", "high"}


@pytest.mark.parametrize("gap", ["missing_window", "actual_end", "phase_id"])
def test_merge_requires_adjacent_native_bounds_same_phase_instance_and_direction(gap):
    motion, reference = inputs([1, 2, 3])
    if gap == "missing_window":
        del motion["windows"][1]
    elif gap == "actual_end":
        motion["windows"][0]["native_end_s"] = 4.5
    else:
        motion["windows"][1]["phase_id"] = "different_instance"
        motion["windows"][2]["phase_id"] = "different_instance"
    result = item(review(motion, reference))
    assert result["episode_count"] == 2
    assert result["clips"][0]["episode_window_count"] == 1


def test_zero_iqr_fallbacks_are_explicit_and_do_not_produce_infinite_json():
    motion, reference = phase_inputs(
        {
            "early_phase": ([40], [10, 10, 10]),
            "antrum": ([20], [0, 0, 0]),
            "incus": ([30], [8, 10, 12]),
        }
    )
    result = item(review(motion, reference))
    assert [clip["phase"] for clip in result["clips"]] == ["antrum", "incus", "early_surface"]
    zero = result["clips"][0]
    assert zero["ranking_scale"] is None and zero["normalized_deviation"] is None
    assert zero["ranking_scale_method"] == "constant_zero_reference"
    assert result["clips"][2]["ranking_scale_method"] == "absolute_reference_median"
    assert result["clips"][2]["normalized_deviation"] == 3
    json.dumps(result, allow_nan=False)
    scaled = item(
        review(
            *phase_inputs(
                {
                    "early_phase": ([40], [10, 10, 10]),
                    "antrum": ([20], [0, 0, 0]),
                    "incus": ([30], [8, 10, 12]),
                },
                factor=0.001,
            )
        )
    )
    assert [clip["clip_id"] for clip in result["clips"]] == [
        clip["clip_id"] for clip in scaled["clips"]
    ]


@pytest.mark.parametrize(
    ("q10", "q90", "values", "expected"),
    [
        (2, 8, [0, 5, 10], (6, "central_80_percent_range")),
        (5, 5, [0, 5, 10], (10, "reference_range")),
        (5, 5, [5, 5, 5], (5, "absolute_reference_median")),
    ],
)
def test_collapsed_iqr_uses_recorded_nonzero_reference_scale(q10, q90, values, expected):
    dist = {"median": 5, "q25": 5, "q75": 5 + 1e-10, "q10": q10, "q90": q90, "values": values}
    assert _ranking_scale(dist, DEFAULT_PROTOCOL) == expected


def test_sufficient_reference_guard_is_retained_even_if_comparison_claims_compared():
    motion, reference = inputs([0])
    reference["recordings"] = reference["recordings"][:2]
    for metric in motion["per_phase"]["antrum"]:
        dist = _distribution(reference["recordings"], "antrum", metric)
        motion["per_phase"]["antrum"][metric] = {
            k: v for k, v in dist.items() if k not in ("values", "cumulative", "total_weight")
        }
    reference["artifact_sha256"] = artifact_sha256(reference)
    motion["reference_sha256"] = reference["artifact_sha256"]
    selected = item(review(motion, reference))
    assert selected["status"] == "unavailable"
    assert selected["eligible_window_count"] == 0 and selected["clips"] == []


def test_identical_zero_values_are_not_candidates_or_duplicate_episodes():
    motion, reference = phase_inputs({"antrum": ([0, 0, 2, 4, 3, 0], [0, 0, 0])})
    selected = item(review(motion, reference))
    assert selected["candidate_count"] == 3 and selected["episode_count"] == 1
    assert len(selected["clips"]) == 1
    assert selected["clips"][0]["observed"] == 4
    assert selected["clips"][0]["episode_window_count"] == 3


def test_safety_cap_is_global_across_phases_without_merging_different_structures():
    phases = [
        {"phase_name": "early_surface", "t_start_s": 0, "t_end_s": 10},
        {"phase_name": "antrum", "t_start_s": 10, "t_end_s": 30},
    ]
    events = [
        {
            "event_id": f"event:{i}",
            "event_type": "simulator_overshoot",
            "t_start_s": t,
            "t_end_s": t + 1,
            "anatomy": anatomy,
            "attributes": {"coaching_eligible": True, "min_edt_mm": -depth},
        }
        for i, (t, depth, anatomy) in enumerate(
            [
                (1, 1, "facial_nerve"),
                (2, 5, "facial_nerve"),
                (12, 3, "dura"),
                (13, 4, "facial_nerve"),
                (14, 2, "dura"),
                (15, 6, "dura"),
            ]
        )
    ]
    result = item(
        build_event_review(events, phases, {}, None, None, duration_s=30),
        "critical_structure_violation",
    )
    assert [clip["observed"] for clip in result["clips"]] == [6, 5, 4, 3]
    assert result["candidate_count"] == 6
    assert result["phase_status"]["early_surface"]["selected_count"] == 1
    assert result["phase_status"]["antrum"]["selected_count"] == 3
