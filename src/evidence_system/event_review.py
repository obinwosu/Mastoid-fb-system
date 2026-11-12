"""Session-wide ranked review moments from already bound analysis artifacts.

This is a display selection policy. It does not change event detection, motion
measurements, the saved expert cohort, or the historical coaching contract.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from .motion_comparison import USER_CONTROL_MAX_FRACTION, _distribution, artifact_sha256

PHASES = (("early_surface", "Early Drilling"), ("antrum", "Antrum"), ("incus", "Incus"))
METRICS = (
    ("critical_structure_violation", "Critical Structure Violation", "safety", "mm"),
    ("poor_drill_visibility", "Poor Drill Visibility", "exposure", "s"),
    ("speed_mm_s", "Tool speed", "motion", "mm/s"),
    ("mean_stroke_length_mm", "Stroke length", "motion", "mm"),
    ("removal_rate_mm3_s", "Removal rate", "motion", "mm³/s"),
)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _phase(value: Any) -> str | None:
    name = "early_surface" if value == "early_phase" else value
    return name if name in {p for p, _ in PHASES} else None


def _bounds(row: Mapping) -> tuple[float, float] | None:
    start, end = _number(row.get("t_start_s")), _number(row.get("t_end_s"))
    return (start, end) if start is not None and end is not None and 0 <= start <= end else None


def _assigned_phase(clip: Mapping, phases: Sequence[Mapping]) -> str | None:
    """Prefer a verified original-image anchor, otherwise greatest interval overlap."""
    intervals = [(row, _bounds(row)) for row in phases if _phase(row.get("phase_name"))]
    anchor = _number(clip.get("keyframe_time_s")) if clip.get("keyframe_available") else None
    if anchor is not None:
        for row, bounds in intervals:
            if bounds and bounds[0] <= anchor < bounds[1]:
                return _phase(row.get("phase_name"))
        # The last recorded frame is included in the final phase.
        for row, bounds in reversed(intervals):
            if bounds and anchor == bounds[1]:
                return _phase(row.get("phase_name"))
    start, end = _bounds(clip)
    ranked = []
    for row, bounds in intervals:
        if bounds is None:
            continue
        overlap = max(0.0, min(end, bounds[1]) - max(start, bounds[0]))
        point_inside = start == end and bounds[0] <= start < bounds[1]
        if overlap > 0 or point_inside:
            ranked.append((overlap, -bounds[0], _phase(row.get("phase_name"))))
    return max(ranked)[2] if ranked else None


def _playback(clip: dict, duration_s: float) -> None:
    clip["playback_start_s"] = max(0.0, clip["t_start_s"] - 5.0)
    clip["playback_end_s"] = min(duration_s, clip["t_end_s"] + 5.0)


def _safety_candidates(events: Sequence[Mapping], visibility: Mapping) -> dict[str, list[dict]]:
    output = {key: [] for key, _, category, _ in METRICS if category != "motion"}
    for event in events:
        attributes = event.get("attributes") or {}
        if event.get("event_type") != "simulator_overshoot" or not attributes.get(
            "coaching_eligible", True
        ):
            continue
        for row in attributes.get("coaching_segments") or [event]:
            bounds = _bounds(row)
            if bounds is None:
                continue
            source_id = str(row.get("segment_id") or event.get("event_id"))
            depth = _number(row.get("min_edt_mm", attributes.get("min_edt_mm")))
            output["critical_structure_violation"].append(
                {
                    **deepcopy(dict(row)),
                    "clip_id": f"event_review:violation:{source_id}",
                    "review_clip_id": f"event_review:violation:{source_id}",
                    "t_start_s": bounds[0],
                    "t_end_s": bounds[1],
                    "observed": max(0.0, -depth) if depth is not None else None,
                    "unit": "mm",
                    "source_ids": [str(event.get("event_id")), source_id],
                    "anatomy": row.get("anatomy") or event.get("anatomy"),
                    "keyframe_available": False,
                }
            )
    if visibility.get("available"):
        for row in visibility.get("coaching_episodes") or []:
            bounds = _bounds(row)
            if bounds is None:
                continue
            source_id = str(row["episode_id"])
            output["poor_drill_visibility"].append(
                {
                    **deepcopy(dict(row)),
                    "clip_id": f"event_review:visibility:{source_id}",
                    "review_clip_id": f"event_review:visibility:{source_id}",
                    "t_start_s": bounds[0],
                    "t_end_s": bounds[1],
                    "observed": bounds[1] - bounds[0],
                    "unit": "s",
                    "source_ids": [source_id],
                    "keyframe_available": False,
                }
            )
    return output


def _matched_distributions(
    motion: Mapping | None, reference: Mapping | None
) -> tuple[dict, str | None]:
    # The legacy available flag counts speed only. Removal/length can still be
    # evaluable when no speed windows qualify; each metric keeps its own gate.
    if not motion or not motion.get("reference_sha256"):
        return {}, (motion or {}).get("reason") or "current_motion_unavailable"
    if not reference or reference.get("schema") != "motion_expert_reference_v1":
        return {}, "matching_window_reference_unavailable"
    protocol = motion.get("protocol") or {}
    if (
        protocol.get("window_s") != 5.0
        or protocol.get("stride_s") != 5.0
        or protocol.get("short_phase_policy")
    ):
        return {}, "requires_nonoverlapping_5s_windows"
    if (
        protocol != reference.get("protocol")
        or motion.get("reference_sha256") != reference.get("artifact_sha256")
        or reference.get("artifact_sha256") != artifact_sha256(reference)
    ):
        return {}, "motion_reference_identity_or_protocol_mismatch"
    participant, run_id = motion.get("participant_id"), motion.get("global_run_id")
    excluded = {
        row["global_run_id"]
        for row in reference["recordings"]
        if row["global_run_id"] == run_id
        or (participant is not None and row["participant_id"] == participant)
    }
    if excluded != set((motion.get("reference_summary") or {}).get("excluded_recording_ids", [])):
        return {}, "motion_reference_self_exclusion_mismatch"
    included = [row for row in reference["recordings"] if row["global_run_id"] not in excluded]
    result = {}
    # Reuse the exact comparison routine: equal participant, recording, then window.
    for phase, _ in PHASES:
        source_phase = "early_phase" if phase == "early_surface" else phase
        for metric, _, category, _ in METRICS:
            if category != "motion":
                continue
            dist = _distribution(included, source_phase, metric)
            saved = (motion.get("per_phase", {}).get(source_phase) or {}).get(metric)
            if saved is None:
                continue
            if any(
                saved.get(key) != dist[key]
                for key in (
                    "median",
                    "q10",
                    "q25",
                    "q75",
                    "q90",
                    "participant_n",
                    "recording_n",
                    "window_n",
                )
            ):
                return {}, "motion_reference_distribution_mismatch"
            result[phase, metric] = dist
    return result, None


def _public_reference(dist: Mapping, unit: str) -> dict:
    values, cumulative, total = dist["values"], dist["cumulative"], dist["total_weight"]
    # A fixed weighted-quantile grid bounds payload size without reweighting the cohort.
    points = (
        [
            values[min(len(values) - 1, max(0, bisect_left(cumulative, i / 100 * total) - 1))]
            for i in range(101)
        ]
        if values
        else []
    )
    return {
        **{
            key: dist[key]
            for key in (
                "median",
                "q10",
                "q25",
                "q75",
                "q90",
                "participant_n",
                "recording_n",
                "window_n",
            )
        },
        "available": bool(values),
        "unit": unit,
        "points": points,
        "display_sampling": "101 weighted quantiles; display samples are not independent experts",
        "comparison_basis": "same_phase_5s_windows",
        "weighting": "equal participant, then recording, then eligible window",
    }


def _motion_clips(
    motion: Mapping, phase: str, metric: str, unit: str, dist: Mapping
) -> tuple[list[dict], int, int]:
    protocol = motion["protocol"]
    scale, scale_method = _ranking_scale(dist, protocol)
    reference = _public_reference(dist, unit)
    eligible_count, candidates, streaks, streak = 0, 0, [], []
    phase_windows = sorted(
        (w for w in motion.get("windows", []) if _phase(w.get("phase")) == phase),
        key=lambda row: (row["native_start_s"], row["window_id"]),
    )
    for row in phase_windows:
        comparison = (row.get("comparisons") or {}).get(metric) or {}
        observed = _number(comparison.get("observed"))
        valid = (
            comparison.get("status") == "compared"
            and dist["participant_n"] >= protocol["min_expert_participants"]
            and all(_number(dist[key]) is not None for key in ("q25", "median", "q75"))
            and row.get("display_clock_valid")
            and (observed is not None and _bounds(row) is not None)
        )
        if metric == "speed_mm_s":
            valid = valid and (row.get("user_control_fraction") or 0) < USER_CONTROL_MAX_FRACTION
        eligible_count += bool(valid)
        direction = None
        if valid:
            tolerance = (
                protocol["percentile_tie_abs_tolerance"]
                + abs(observed) * protocol["percentile_tie_rel_tolerance"]
            )
            if observed + tolerance < dist["q25"]:
                direction = "low"
            elif observed - tolerance > dist["q75"]:
                direction = "high"
        contiguous = (
            bool(streak)
            and direction == streak[-1]["direction"]
            and (
                row["phase_id"] == streak[-1]["phase_id"]
                and abs(row["native_start_s"] - streak[-1]["native_end_s"]) < 1e-7
            )
        )
        if streak and (direction is None or not contiguous):
            streaks.append(streak)
            streak = []
        if direction:
            candidates += 1
            streak.append(
                {
                    "clip_id": f"event_review:{metric}:{row['window_id']}",
                    "t_start_s": row["t_start_s"],
                    "t_end_s": row["t_end_s"],
                    "native_start_s": row["native_start_s"],
                    "native_end_s": row["native_end_s"],
                    "phase": phase,
                    "phase_id": row["phase_id"],
                    "observed": observed,
                    "unit": unit,
                    "deviation": abs(observed - dist["median"]),
                    "normalized_deviation": (
                        abs(observed - dist["median"]) / scale if scale is not None else None
                    ),
                    "ranking_scale": scale,
                    "ranking_scale_method": scale_method,
                    "reference": reference,
                    "direction": direction,
                    "comparison": deepcopy(comparison),
                    "window_id": row["window_id"],
                    "source_ids": [row["window_id"]],
                    "keyframe_available": False,
                }
            )
    if streak:
        streaks.append(streak)
    clips = []
    for episode in streaks:
        selected = deepcopy(min(episode, key=_motion_rank))
        selected["source_ids"] = [clip["window_id"] for clip in episode]
        selected["episode_window_count"] = len(episode)
        selected["episode_start_s"] = episode[0]["t_start_s"]
        selected["episode_end_s"] = episode[-1]["t_end_s"]
        clips.append(selected)
    return (
        sorted(clips, key=_motion_rank),
        candidates,
        eligible_count,
    )


def _ranking_scale(dist: Mapping, protocol: Mapping) -> tuple[float | None, str]:
    """Avoid dividing by a collapsed IQR; retain explicit fallback provenance.

    Fallbacks change the normalization basis, never the quartile eligibility.
    A constant zero reference has no empirical scale. Its eligible departures
    sort ahead of finite normalized scores, with absolute departure breaking
    ties; JSON carries null instead of a fabricated scale or infinite score.
    """
    median = dist["median"]
    if median is None:
        return None, "no_reference"
    tolerance = (
        protocol["percentile_tie_abs_tolerance"]
        + abs(median) * protocol["percentile_tie_rel_tolerance"]
    )
    values = dist["values"]
    for value, method in (
        (dist["q75"] - dist["q25"], "iqr"),
        (dist["q90"] - dist["q10"], "central_80_percent_range"),
        (values[-1] - values[0], "reference_range"),
        (abs(median), "absolute_reference_median"),
    ):
        if value > tolerance:
            return value, method
    return None, "constant_zero_reference"


def _motion_rank(clip: Mapping) -> tuple:
    score = clip["normalized_deviation"]
    return (
        0 if score is None else 1,
        -clip["deviation"] if score is None else -score,
        clip["t_start_s"],
        clip["clip_id"],
    )


def build_event_review(
    events: Sequence[Mapping],
    phases: Sequence[Mapping],
    visibility: Mapping,
    motion: Mapping | None,
    reference: Mapping | None,
    *,
    duration_s: float,
    enrich_keyframes: Callable[[Mapping], None] | None = None,
) -> dict:
    """Select up to four episodes per category across the complete session."""
    safety = _safety_candidates(events, visibility)
    if enrich_keyframes:
        enrich_keyframes(
            {
                "items": [
                    {"feedback_id": f"event_review:{metric}", "review_clips": clips}
                    for metric, clips in safety.items()
                ]
            }
        )
    for clips in safety.values():
        for clip in clips:
            clip["phase"] = _assigned_phase(clip, phases)
    distributions, reason = _matched_distributions(motion, reference)
    result = {
        "schema": "session_event_review_v2",
        "phases": [{"phase": phase, "label": label} for phase, label in PHASES],
        "items": [],
        "policy": {
            "top_n": 4,
            "scope": "session",
            "maximum_selected_moments": 20,
            "window_s": 5,
            "stride_s": 5,
            "motion_rank": "absolute difference from matched same-phase expert median / IQR",
            "motion_threshold": "strictly outside weighted Q25-Q75, beyond numerical tolerance",
            "relative_difference_threshold": None,
            "ranking_scale_fallback": (
                "if IQR is within numerical tolerance: central 80 percent range, reference "
                "range, absolute reference median; a constant zero reference has null scale/score "
                "and sorts before finite scores, by absolute departure then time and clip ID"
            ),
            "episode_selection": (
                "most normalized-deviant window per contiguous same-phase, same-direction "
                "candidate episode, before session-wide ranking"
            ),
            "safety_rank": "maximum intrusion; visibility episode duration",
            "context_padding_s": 5,
            "threshold_status": "provisional_descriptive_review_policy_not_clinically_validated",
        },
    }
    for metric, title, category, unit in METRICS:
        item = {
            "feedback_id": f"event_review:session:{metric}",
            "phase": "session",
            "category": category,
            "group": "motion" if category == "motion" else "safety",
            "metric_key": metric,
            "title": title,
            "unit": unit,
            "clips": [],
            "reference": None,
            "status": "no_candidates",
            "reason": None,
            "candidate_count": 0,
            "episode_count": 0,
            "eligible_window_count": 0,
            "phase_status": {},
        }
        candidates = []
        for phase, _ in PHASES:
            phase_status = {
                "status": "no_candidates",
                "reason": None,
                "candidate_count": 0,
                "episode_count": 0,
                "eligible_window_count": 0,
                "selected_count": 0,
            }
            if category == "motion":
                dist = distributions.get((phase, metric))
                if reason or dist is None:
                    phase_status.update(
                        status="unavailable", reason=reason or "metric_comparison_unavailable"
                    )
                    clips = []
                else:
                    clips, count, eligible = _motion_clips(motion, phase, metric, unit, dist)
                    phase_status.update(candidate_count=count, eligible_window_count=eligible)
                    if not eligible:
                        phase_status.update(
                            status="unavailable",
                            reason="no_eligible_windows_with_sufficient_reference",
                        )
            else:
                clips = [clip for clip in safety[metric] if clip["phase"] == phase]
                phase_status["candidate_count"] = len(clips)
                if category == "exposure" and not visibility.get("available"):
                    phase_status.update(
                        status="unavailable",
                        reason=visibility.get("reason") or "visibility_unavailable",
                    )
            phase_status["episode_count"] = len(clips)
            if clips:
                phase_status.update(status="candidates_available", reason=None)
            item["phase_status"][phase] = phase_status
            for key in ("candidate_count", "episode_count", "eligible_window_count"):
                item[key] += phase_status[key]
            candidates.extend(clips)
        if category == "motion":
            candidates.sort(key=_motion_rank)
        else:
            candidates.sort(
                key=lambda clip: (
                    -(clip["observed"] if clip["observed"] is not None else -math.inf),
                    clip["t_start_s"],
                    clip["clip_id"],
                )
            )
        for rank, clip in enumerate(candidates[:4], 1):
            clip["rank"] = rank
            _playback(clip, duration_s)
            item["clips"].append(clip)
            item["phase_status"][clip["phase"]]["selected_count"] += 1
        if item["clips"]:
            item.update(status="ready", reason=None)
        elif all(status["status"] == "unavailable" for status in item["phase_status"].values()):
            reasons = {status["reason"] for status in item["phase_status"].values()}
            item.update(
                status="unavailable",
                reason=next(iter(reasons)) if len(reasons) == 1 else "phase_data_unavailable",
            )
        result["items"].append(item)
    return result
