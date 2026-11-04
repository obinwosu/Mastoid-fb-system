"""Descriptive native-time vision-pose windows and participant-balanced references.

Speed/removal measurements are independent of legacy stroke segmentation. An
optional reference extension preserves recorded stroke counts and their limits.
A low relative speed never establishes that drilling should be faster.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


WINDOW_SCHEMA = "motion_windows_v1"
REFERENCE_SCHEMA = "motion_expert_reference_v1"
COMPARISON_SCHEMA = "motion_comparison_v1"
PHASES = ("early_phase", "antrum", "incus")
PHASE_NAMES = {"early_phase": "early_surface", "antrum": "antrum", "incus": "incus"}
DEFAULT_PROTOCOL = {
    "schema": "motion_window_protocol_v1",
    "pose_source": "canonical_vision_pose", "position_unit": "m",
    "time_basis": "native_elapsed_seconds", "coarse_phase_taxonomy": list(PHASES),
    "window_s": 5.0, "stride_s": 1.0, "max_gap_s": 0.25,
    "min_coverage": 0.8, "min_expert_participants": 3,
    "low_percentile": 10.0, "min_relative_deficit": 0.25,
    "persistence_windows": 3, "max_display_clock_step_s": 1.0,
    "percentile_tie_abs_tolerance": 1e-9, "percentile_tie_rel_tolerance": 1e-12,
    "threshold_status": "provisional_descriptive_review_policy_not_clinically_validated",
    "policy_date": "2026-11-04",
}
CLAIM_BOUNDARY = (
    "This is a descriptive comparison with experienced participants, not a skill score, "
    "clinical abnormality threshold, or instruction to increase drilling speed. "
    "Overlapping windows are correlated. Anatomy and assistance conditions are pooled; "
    "manual reference phases and predicted current phases can differ in accuracy."
)
STROKE_FREQUENCY_CONTRACT = {
    "schema": "recorded_stroke_frequency_v1",
    "source": "unchanged recorded stroke_id/stroke_end_flag/is_drilling_frame; legacy k-cosine k=3",
    "time_basis": "native elapsed seconds, not resampled video time",
    "stroke_frequency_hz": "stroke_end_count on (start,end] / full window_s (5 seconds)",
    "stroke_rate_per_s": "distinct drilling stroke IDs on [start,end] / stroke_drilling_time_s",
    "stroke_drilling_time_s": "sum min(dt,1s) between observed rows inside [start,end] whose right row is_drilling_frame",
    "legacy_adaptation": "legacy unique-ID/drilling-time formula on native 5s windows; original phase statistic used t_video",
    "eligibility": "pose coverage >= min_coverage and finite legacy turn angles; drilling-time rate also needs >=5 drilling rows and positive denominator",
    "limitations": "recorded segmentation is not repaired; source strokes may span gaps; drilling-frame mask is not measured motor-on time; crossing strokes count by overlap for stroke_rate_per_s and by end for stroke_frequency_hz",
    "usage": "saved reference measurements only; no frequency coaching threshold or live comparison added",
}
STROKE_LENGTH_CONTRACT = {
    "schema": "complete_recorded_stroke_length_v1", "unit": "mm",
    "length": "sum Euclidean steps within a recorded drilling stroke_id, metres multiplied by 1000",
    "assignment": "whole stroke assigned by endpoint on (window_start,window_end]; never clip its path to the window",
    "window_statistic": "arithmetic mean of eligible complete stroke lengths ending in the window; no endpoints means null",
    "eligibility": "finite legacy segmentation; >=2 points; one terminal flag; not the forced last boundary; contiguous selected ID; no full-span phase change; selected gaps <= motion max_gap_s",
    "coverage": "window also requires motion min_coverage; source exclusions remain visible in the stroke table",
    "limitation": "recorded segmentation is reused, not repaired; endpoint windows overlap and can repeat strokes",
}
STROKE_LENGTH_REVIEW_POLICY = {
    "schema": "stroke_length_review_policy_v1",
    "candidate": "mean_stroke_length_mm strictly below same-phase weighted expert Q25",
    "persistence_windows": 3,
    "status": "provisional_descriptive_review_policy_not_clinically_validated",
    "frequency_gate": False,
}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def artifact_sha256(payload: Mapping[str, Any]) -> str:
    """Content digest independent of JSON whitespace and its own digest field."""
    return _digest({key: value for key, value in payload.items() if key != "artifact_sha256"})


def _seal(payload: dict) -> dict:
    payload["artifact_sha256"] = artifact_sha256(payload)
    return payload


def _sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _protocol(value: Mapping | None) -> dict:
    result = deepcopy(DEFAULT_PROTOCOL if value is None else dict(value))
    if set(result) != set(DEFAULT_PROTOCOL):
        raise ValueError("Motion protocol fields differ from the supported version")
    fixed = ("schema", "pose_source", "position_unit", "time_basis", "coarse_phase_taxonomy",
             "window_s", "stride_s", "threshold_status", "policy_date",
             "percentile_tie_abs_tolerance", "percentile_tie_rel_tolerance")
    if any(result[key] != DEFAULT_PROTOCOL[key] for key in fixed):
        raise ValueError("Unsupported motion source, taxonomy, time basis, or window contract")
    for key in ("max_gap_s", "min_coverage", "low_percentile", "min_relative_deficit",
                "max_display_clock_step_s"):
        if _number(result[key]) is None:
            raise ValueError(f"Motion protocol {key} must be finite")
    if not (0 < result["max_gap_s"] < result["window_s"] and 0 < result["min_coverage"] <= 1
            and 0 <= result["low_percentile"] <= 100 and 0 <= result["min_relative_deficit"] <= 1
            and result["max_display_clock_step_s"] > 0):
        raise ValueError("Invalid motion protocol thresholds")
    for key in ("min_expert_participants", "persistence_windows"):
        if type(result[key]) is not int or result[key] < 1:
            raise ValueError(f"Motion protocol {key} must be a positive integer")
    return result


def _provenance(value: Mapping) -> dict:
    result = deepcopy(dict(value))
    if result.get("pose_source") != "canonical_vision_pose" or result.get("position_unit") != "m":
        raise ValueError("Motion requires canonical vision pose with explicitly declared metre positions")
    if not result.get("phase_source_kind") or not result.get("clock_source"):
        raise ValueError("Motion requires explicit phase and clock provenance")
    if result["phase_source_kind"] == "model_phase_prediction" and not _sha(result.get("phase_model_sha256")):
        raise ValueError("Predicted-phase motion requires its checkpoint SHA256")
    sources = result.get("source_artifacts")
    if (not isinstance(sources, list) or any(not isinstance(s, Mapping) for s in sources)
            or not {"frames", "phases"}.issubset({s.get("kind") for s in sources})):
        raise ValueError("Motion provenance requires frame and phase source artifacts")
    for source in sources:
        if not source.get("path") or not _sha(source.get("sha256")):
            raise ValueError("Motion source artifacts require a path and SHA256")
    return result


def _phase(row: Mapping) -> str | None:
    aliases = {"early_surface": "early_phase", "early_phase": "early_phase",
               "pre_antrum": "early_phase", "antrum": "antrum", "incus": "incus",
               "post_incus": "incus"}
    value = row.get("model_phase") or row.get("phase")
    return aliases.get(str(value))


def _interpolate(time: float, native: list[float], video: list[float]) -> float:
    index = min(len(native) - 2, max(0, bisect_right(native, time) - 1))
    fraction = (time - native[index]) / (native[index + 1] - native[index])
    return video[index] + fraction * (video[index + 1] - video[index])


def build_motion_windows(
    frames: Sequence[Mapping[str, Any]], *, global_run_id: str,
    participant_id: str | None = None, provenance: Mapping, protocol: Mapping | None = None,
) -> dict:
    """Measure full 5s windows within each contiguous observed phase interval.

    All observed vision rows remain present, including stationary/non-removal rows.
    Path uses linear clipping of *valid short* adjacent pose segments only. Removal
    uses frame-assigned counts on (start,end], omitting counts whose preceding
    interval is missing/too long. Its denominator is always the full five seconds;
    no removal events are invented or proportionally redistributed at boundaries.
    """
    policy, source = _protocol(protocol), _provenance(provenance)
    if not frames or not str(global_run_id).strip():
        raise ValueError("Motion windows require frames and a recording identity")
    rows = []
    derived_id = None
    derived_count = 0
    for frame in frames:
        required = ("t_abs", "t_video_s", "drill_x", "drill_y", "drill_z", "frame_removed_mm3")
        if any(key not in frame for key in required):
            raise ValueError("Motion input lacks canonical vision position/time/removal fields")
        absolute, video = _number(frame["t_abs"]), _number(frame["t_video_s"])
        if absolute is None or video is None:
            raise ValueError("Motion timestamps must be finite")
        if rows and (absolute <= rows[-1]["t_abs"] or video < rows[-1]["t_video_s"]):
            raise ValueError("Motion native timestamps must increase and video times cannot reverse")
        phase = _phase(frame)
        phase_id = str(frame.get("phase_id") or "")
        if phase is not None and not phase_id:
            if derived_id is None or not rows or rows[-1]["phase"] != phase:
                derived_id = f"{global_run_id}:motion_observed_phase:{len(rows):06d}"
            phase_id = derived_id
            derived_count += 1
        else:
            derived_id = None
        removal = _number(frame["frame_removed_mm3"])
        if removal is not None and removal < 0:
            raise ValueError("Motion removal counts cannot be negative")
        rows.append({"t_abs": absolute, "t_video_s": video, "phase": phase, "phase_id": phase_id,
                     "position_m": [_number(frame[key]) for key in ("drill_x", "drill_y", "drill_z")],
                     "removed_mm3": removal})
    native = [r["t_abs"] - rows[0]["t_abs"] for r in rows]
    video = [r["t_video_s"] for r in rows]
    windows = []
    run_start = 0
    while run_start < len(rows):
        run_end = run_start + 1
        identity = (rows[run_start]["phase"], rows[run_start]["phase_id"])
        while run_end < len(rows) and (rows[run_end]["phase"], rows[run_end]["phase_id"]) == identity:
            run_end += 1
        if identity[0] is not None:
            span = native[run_end - 1] - native[run_start]
            count = max(0, math.floor((span - policy["window_s"] + 1e-9) / policy["stride_s"]) + 1)
            for ordinal in range(count):
                start = native[run_start] + ordinal * policy["stride_s"]
                end = start + policy["window_s"]
                first = max(run_start, bisect_right(native, start) - 1)
                last = min(run_end - 1, bisect_left(native, end))
                path = valid = stationary = removal_valid = removed = excluded = 0.0
                valid_segments = 0
                display_valid = True
                for index in range(first + 1, last + 1):
                    dt = native[index] - native[index - 1]
                    overlap = max(0.0, min(end, native[index]) - max(start, native[index - 1]))
                    if not overlap:
                        continue
                    short = dt <= policy["max_gap_s"] + 1e-9
                    display_valid &= video[index] - video[index - 1] <= policy["max_display_clock_step_s"] + 1e-9
                    previous, current = rows[index - 1], rows[index]
                    if short and all(v is not None for v in previous["position_m"] + current["position_m"]):
                        distance = math.dist(previous["position_m"], current["position_m"]) * 1000.0
                        path += distance * overlap / dt
                        valid += overlap
                        valid_segments += 1
                        if distance == 0:
                            stationary += overlap
                    value = current["removed_mm3"]
                    if short and value is not None:
                        removal_valid += overlap
                        if start < native[index] <= end:
                            removed += value
                    elif value is not None and start < native[index] <= end:
                        excluded += value
                t_start, t_end = _interpolate(start, native, video), _interpolate(end, native, video)
                coverage = min(1.0, valid / policy["window_s"])
                removal_coverage = min(1.0, removal_valid / policy["window_s"])
                display_valid = bool(display_valid and t_end > t_start)
                windows.append({
                    "window_id": f"{global_run_id}:motion:{len(windows) + 1:06d}",
                    "phase": identity[0], "phase_name": PHASE_NAMES[identity[0]], "phase_id": identity[1],
                    "native_start_s": start, "native_end_s": end, "t_start_s": t_start, "t_end_s": t_end,
                    "source_start_row": first, "source_end_row": last,
                    "valid_segment_count": valid_segments, "valid_duration_s": valid,
                    "coverage_fraction": coverage, "path_length_mm": path,
                    "speed_mm_s": path / valid if valid > 0 else None,
                    "stationary_duration_s": stationary,
                    "stationary_fraction": stationary / valid if valid > 0 else None,
                    "removed_mm3": removed, "removal_rate_mm3_s": removed / policy["window_s"],
                    "removal_valid_duration_s": removal_valid, "removal_coverage_fraction": removal_coverage,
                    "excluded_gap_removal_mm3": excluded,
                    "eligible": coverage + 1e-9 >= policy["min_coverage"],
                    "removal_eligible": removal_coverage + 1e-9 >= policy["min_coverage"],
                    "display_clock_valid": display_valid,
                })
        run_start = run_end
    return _seal({
        "schema": WINDOW_SCHEMA, "global_run_id": str(global_run_id),
        "participant_id": str(participant_id) if participant_id is not None else None,
        "protocol": policy, "protocol_sha256": _digest(policy), "provenance": source,
        "input_rows_sha256": _digest(rows), "source_frame_count": len(rows),
        "derived_phase_id_row_count": derived_count,
        "native_time_origin_abs": rows[0]["t_abs"], "native_duration_s": native[-1],
        "windows": windows, "window_count": len(windows),
        "eligible_window_count": sum(w["eligible"] for w in windows),
        "measurement_contract": {
            "window_grid": "5 native seconds, stride1, anchored at each contiguous observed phase's first row",
            "speed": "sum valid clipped adjacent path mm / valid observed native seconds",
            "coverage": "valid adjacent native seconds / full5seconds; never bridge long gaps",
            "removal": "frame-assigned counts on(start,end], omit long/missing predecessor intervals; divide by full5seconds",
            "boundary_counts": "frame endpoint assignment, no subframe removal interpolation",
            "stationary": "exactly zero observed displacement, not inferred from absent removal",
            "phase": "no cross-phase_id segments or windows; predrilling and unknown phases excluded",
            "missing_phase_id": "deterministic identities for contiguous existing labels; no missing phase labels inferred",
            "display": "native window endpoints mapped to video clock only for review clips",
            "pose": "all vision tool positions; not restricted to contact or removal frames",
        }, "claim_boundary": CLAIM_BOUNDARY,
    })


def _recorded_stroke_source(window_build: Mapping, frames: Sequence[Mapping]):
    """Parse recorded boundaries and diagnose the unchanged source segmentation."""
    import numpy as np

    validate_window_build(window_build)
    if len(frames) != window_build["source_frame_count"]:
        raise ValueError("Stroke frequency source frame count differs")

    def flag(value):
        if isinstance(value, str) and value.casefold() in {"true", "false"}:
            return value.casefold() == "true"
        number = _number(value)
        if number not in (0, 1):
            raise ValueError("Missing or invalid source stroke flag")
        return bool(number)

    rows = []
    for frame in frames:
        time = _number(frame.get("t_abs"))
        drilling, end = flag(frame.get("is_drilling_frame")), flag(frame.get("stroke_end_flag"))
        stroke = _number(frame.get("stroke_id")) if drilling else None
        position = [_number(frame.get(key)) for key in ("drill_x", "drill_y", "drill_z")]
        if (time is None or (rows and time <= rows[-1][0]) or (end and not drilling)
                or (drilling and (stroke is None or stroke < 0 or not stroke.is_integer()
                                  or any(v is None for v in position)))):
            raise ValueError("Invalid source stroke identity, pose or native time")
        rows.append((time, drilling, end, int(stroke) if drilling else None, position))
    native = [r[0] - rows[0][0] for r in rows]
    if (rows[0][0] != window_build["native_time_origin_abs"]
            or native[-1] != window_build["native_duration_s"]):
        raise ValueError("Stroke frequency source native clock differs")
    selected = [r for r in rows if r[1]]
    nonfinite = 0
    status = "insufficient_drilling_frames"
    if len(selected) >= 7:
        positions = np.asarray([r[4] for r in selected], dtype=float)
        before, after = positions[3:-3] - positions[:-6], positions[3:-3] - positions[6:]
        with np.errstate(divide="ignore", invalid="ignore"):
            angles = np.arccos(np.sum(before * after, axis=1)
                               / (np.linalg.norm(before, axis=1) * np.linalg.norm(after, axis=1)))
        nonfinite = int(np.count_nonzero(~np.isfinite(angles)))
        status = "nonfinite_legacy_turn_angles" if nonfinite else "recorded_legacy_segmentation"
    metadata = {
        "schema": STROKE_FREQUENCY_CONTRACT["schema"], "contract": deepcopy(STROKE_FREQUENCY_CONTRACT),
        "input_sha256": _digest(rows), "source_status": status,
        "nonfinite_turn_angle_count": nonfinite,
        "source_stroke_count": len({r[3] for r in selected}),
        "source_drilling_frame_count": len(selected),
    }
    return rows, native, metadata


def add_stroke_frequency(window_build: Mapping, frames: Sequence[Mapping]) -> dict:
    """Add both source-defined rates to an existing reference without resegmentation."""
    if "stroke_frequency_metadata" in window_build:
        raise ValueError("Stroke frequency is already present")
    rows, native, metadata = _recorded_stroke_source(window_build, frames)
    good_source = metadata["source_status"] == "recorded_legacy_segmentation"
    result = deepcopy(dict(window_build))
    result["stroke_frequency_metadata"] = metadata
    for window in result["windows"]:
        start, end = window["native_start_s"], window["native_end_s"]
        left, right = bisect_left(native, start), bisect_right(native, end)
        active = [r for r in rows[left:right] if r[1]]
        count = len({r[3] for r in active})
        ends = sum(rows[i][2] for i in range(bisect_right(native, start), right))
        duration = sum(min(native[i] - native[i - 1], 1.)
                       for i in range(left + 1, right) if rows[i][1])
        window.update(stroke_count=count, stroke_end_count=ends,
                      stroke_drilling_frame_count=len(active), stroke_drilling_time_s=duration,
                      stroke_frequency_hz=ends / result["protocol"]["window_s"],
                      stroke_rate_per_s=count / duration if duration > 0 else None,
                      stroke_frequency_eligible=bool(window["eligible"] and good_source),
                      stroke_rate_eligible=bool(window["eligible"] and good_source and len(active) >= 5 and duration > 0))
    _seal(result)
    validate_window_build(result)
    return result


def add_stroke_length(window_build: Mapping, frames: Sequence[Mapping]) -> dict:
    """Measure complete recorded paths once, then assign them by endpoint."""
    if "stroke_length_metadata" in window_build:
        raise ValueError("Stroke length is already present")
    rows, native, source = _recorded_stroke_source(window_build, frames)
    identities, derived = [], None
    for index, frame in enumerate(frames):
        phase, phase_id = _phase(frame), frame.get("phase_id")
        if phase is not None and not phase_id:
            if derived is None or not identities or identities[-1][0] != phase:
                derived = f"{window_build['global_run_id']}:motion_observed_phase:{index:06d}"
            phase_id = derived
        else:
            derived = None
        identities.append((phase, phase_id))
    groups: dict[int, list[int]] = {}
    selected = [i for i, row in enumerate(rows) if row[1]]
    order = {index: ordinal for ordinal, index in enumerate(selected)}
    for index in selected:
        groups.setdefault(rows[index][3], []).append(index)
    strokes = []
    for stroke_id, indices in groups.items():
        first, last = indices[0], indices[-1]
        reasons = []
        if len(indices) < 2:
            reasons.append("insufficient_points")
        if sum(rows[i][2] for i in indices) != 1 or not rows[last][2]:
            reasons.append("missing_terminal_flag")
        if last == selected[-1]:
            reasons.append("forced_terminal_boundary")
        if source["source_status"] != "recorded_legacy_segmentation":
            reasons.append("source_segmentation_invalid")
        if order[last] - order[first] + 1 != len(indices):
            reasons.append("noncontiguous_stroke_id")
        phase, phase_id = identities[last]
        if phase is None or len(set(identities[first:last + 1])) != 1:
            reasons.append("phase_boundary")
        gap = max((native[j] - native[i] for i, j in zip(indices, indices[1:])), default=0.)
        if gap > window_build["protocol"]["max_gap_s"] + 1e-9:
            reasons.append("unsupported_gap")
        length = sum(math.dist(rows[i][4], rows[j][4]) * 1000 for i, j in zip(indices, indices[1:]))
        strokes.append({
            "stroke_id": stroke_id, "source_start_row": first, "source_end_row": last,
            "native_start_s": native[first], "native_end_s": native[last],
            "length_mm": length, "phase": phase, "phase_id": phase_id,
            "max_gap_s": gap, "eligible": not reasons, "exclusion_reasons": reasons,
        })
    result = deepcopy(dict(window_build))
    result["stroke_length_metadata"] = {
        "schema": STROKE_LENGTH_CONTRACT["schema"], "contract": deepcopy(STROKE_LENGTH_CONTRACT),
        "input_sha256": _digest({"rows": rows, "phases": identities}),
        "source_status": source["source_status"],
        "nonfinite_turn_angle_count": source["nonfinite_turn_angle_count"],
    }
    result["stroke_lengths"] = strokes
    for window in result["windows"]:
        candidates = [s for s in strokes if window["native_start_s"] < s["native_end_s"] <= window["native_end_s"]
                      and s["phase_id"] == window["phase_id"] and s["phase"] == window["phase"]]
        eligible = [s for s in candidates if s["eligible"]]
        window.update(
            mean_stroke_length_mm=sum(s["length_mm"] for s in eligible) / len(eligible) if eligible else None,
            stroke_length_count=len(eligible), stroke_length_candidate_count=len(candidates),
            stroke_length_ids=[s["stroke_id"] for s in eligible],
            stroke_length_eligible=bool(window["eligible"] and eligible),
        )
    _seal(result)
    validate_window_build(result)
    return result


def validate_window_build(payload: Mapping) -> None:
    if payload.get("schema") != WINDOW_SCHEMA or payload.get("artifact_sha256") != artifact_sha256(payload):
        raise ValueError("Invalid motion window schema or content digest")
    policy = _protocol(payload.get("protocol"))
    _provenance(payload["provenance"])
    if payload.get("protocol_sha256") != _digest(policy) or not _sha(payload.get("input_rows_sha256")):
        raise ValueError("Motion protocol/input digest mismatch")
    windows = payload.get("windows")
    if not isinstance(windows, list) or payload.get("window_count") != len(windows):
        raise ValueError("Motion window inventory mismatch")
    if not payload.get("global_run_id") or type(payload.get("source_frame_count")) is not int or payload["source_frame_count"] < 1:
        raise ValueError("Motion source identity/frame inventory missing")
    if _number(payload.get("native_duration_s")) is None or payload["native_duration_s"] < 0:
        raise ValueError("Motion source duration is invalid")
    if payload.get("eligible_window_count") != sum(bool(row.get("eligible")) for row in windows):
        raise ValueError("Motion eligible window inventory mismatch")
    stroke = payload.get("stroke_frequency_metadata")
    if stroke is not None:
        if (stroke.get("schema") != STROKE_FREQUENCY_CONTRACT["schema"]
                or stroke.get("contract") != STROKE_FREQUENCY_CONTRACT
                or not _sha(stroke.get("input_sha256"))
                or stroke.get("source_status") not in {"recorded_legacy_segmentation", "nonfinite_legacy_turn_angles", "insufficient_drilling_frames"}):
            raise ValueError("Invalid stroke frequency source contract")
        for key in ("nonfinite_turn_angle_count", "source_stroke_count", "source_drilling_frame_count"):
            if type(stroke.get(key)) is not int or stroke[key] < 0:
                raise ValueError("Invalid stroke frequency source counts")
        expected_status = ("insufficient_drilling_frames" if stroke["source_drilling_frame_count"] < 7
                           else "nonfinite_legacy_turn_angles" if stroke["nonfinite_turn_angle_count"]
                           else "recorded_legacy_segmentation")
        if stroke["source_status"] != expected_status:
            raise ValueError("Stroke frequency source status disagrees with diagnostics")
    length = payload.get("stroke_length_metadata")
    if length is not None:
        if (length.get("schema") != STROKE_LENGTH_CONTRACT["schema"]
                or length.get("contract") != STROKE_LENGTH_CONTRACT
                or not _sha(length.get("input_sha256"))
                or not isinstance(payload.get("stroke_lengths"), list)):
            raise ValueError("Invalid stroke length source contract")
        seen_strokes = set()
        for item in payload["stroke_lengths"]:
            sid = item.get("stroke_id")
            if type(sid) is not int or sid < 0 or sid in seen_strokes:
                raise ValueError("Invalid or repeated stroke length identity")
            seen_strokes.add(sid)
            if (any(_number(item.get(key)) is None or item[key] < 0
                    for key in ("length_mm", "native_start_s", "native_end_s", "max_gap_s"))
                    or not 0 <= item["native_start_s"] <= item["native_end_s"] <= payload["native_duration_s"]
                    or not isinstance(item.get("exclusion_reasons"), list)
                    or item.get("eligible") is not (not item["exclusion_reasons"])):
                raise ValueError("Invalid stroke length measurements or eligibility")
            if item["eligible"] and (length.get("source_status") != "recorded_legacy_segmentation"
                    or item.get("phase") not in PHASES or not item.get("phase_id")
                    or item["max_gap_s"] > policy["max_gap_s"] + 1e-9):
                raise ValueError("Stroke length eligibility contradicts source quality")
    identities = set()
    for row in windows:
        if row.get("window_id") in identities or not row.get("window_id") or not row.get("phase_id"):
            raise ValueError("Motion window identity is missing or repeated")
        identities.add(row["window_id"])
        if row.get("phase") not in PHASES or row.get("phase_name") != PHASE_NAMES[row["phase"]]:
            raise ValueError("Motion window phase taxonomy mismatch")
        numeric = ("native_start_s", "native_end_s", "t_start_s", "t_end_s", "valid_duration_s",
                   "coverage_fraction", "path_length_mm", "removed_mm3", "removal_rate_mm3_s",
                   "removal_valid_duration_s", "removal_coverage_fraction")
        if any(_number(row.get(key)) is None for key in numeric):
            raise ValueError("Motion window has missing/nonfinite measurements")
        if abs(row["native_end_s"] - row["native_start_s"] - policy["window_s"]) > 1e-7:
            raise ValueError("Motion window does not span five native seconds")
        if row["native_start_s"] < 0 or row["native_end_s"] > payload["native_duration_s"] + 1e-7:
            raise ValueError("Motion window lies outside the observed recording")
        if not (type(row.get("source_start_row")) is int and type(row.get("source_end_row")) is int
                and 0 <= row["source_start_row"] < row["source_end_row"] < payload["source_frame_count"]):
            raise ValueError("Motion window source row bounds are invalid")
        for key in ("coverage_fraction", "removal_coverage_fraction"):
            if not 0 <= row[key] <= 1:
                raise ValueError("Motion coverage outside[0,1]")
        if row["eligible"] != (row["coverage_fraction"] + 1e-9 >= policy["min_coverage"]):
            raise ValueError("Motion eligibility disagrees with coverage")
        if row["removal_eligible"] != (row["removal_coverage_fraction"] + 1e-9 >= policy["min_coverage"]):
            raise ValueError("Removal eligibility disagrees with coverage")
        if row["speed_mm_s"] is not None and (_number(row["speed_mm_s"]) is None or row["speed_mm_s"] < 0):
            raise ValueError("Motion speed must be finite and nonnegative or missing")
        if row["eligible"] and row["speed_mm_s"] is None:
            raise ValueError("Eligible motion window has no speed")
        if min(row["path_length_mm"], row["removed_mm3"], row["removal_rate_mm3_s"]) < 0:
            raise ValueError("Motion/removal measurements cannot be negative")
        for duration, coverage in (("valid_duration_s", "coverage_fraction"),
                                   ("removal_valid_duration_s", "removal_coverage_fraction")):
            if not (0 <= row[duration] <= policy["window_s"] + 1e-7
                    and math.isclose(row[coverage], row[duration] / policy["window_s"], abs_tol=1e-8)):
                raise ValueError("Motion duration and coverage disagree")
        expected_speed = row["path_length_mm"] / row["valid_duration_s"] if row["valid_duration_s"] else None
        if ((expected_speed is None) != (row["speed_mm_s"] is None)
                or (expected_speed is not None and not math.isclose(row["speed_mm_s"], expected_speed, rel_tol=1e-10, abs_tol=1e-10))):
            raise ValueError("Motion speed disagrees with observed path/time")
        if not math.isclose(row["removal_rate_mm3_s"], row["removed_mm3"] / policy["window_s"], rel_tol=1e-10, abs_tol=1e-10):
            raise ValueError("Removal rate must use the full window denominator")
        stationary = _number(row.get("stationary_duration_s"))
        if stationary is None or not 0 <= stationary <= row["valid_duration_s"] + 1e-7:
            raise ValueError("Stationary duration must come from observed valid time")
        if row["display_clock_valid"] and row["t_end_s"] <= row["t_start_s"]:
            raise ValueError("Display-valid motion window has invalid clip bounds")
        if length is not None:
            candidates = [s for s in payload["stroke_lengths"]
                          if row["native_start_s"] < s["native_end_s"] <= row["native_end_s"]
                          and s["phase_id"] == row["phase_id"] and s["phase"] == row["phase"]]
            valid = [s for s in candidates if s["eligible"]]
            mean = sum(s["length_mm"] for s in valid) / len(valid) if valid else None
            value = _number(row.get("mean_stroke_length_mm"))
            if (row.get("stroke_length_ids") != [s["stroke_id"] for s in valid]
                    or row.get("stroke_length_count") != len(valid)
                    or row.get("stroke_length_candidate_count") != len(candidates)
                    or (mean is None and row.get("mean_stroke_length_mm") is not None)
                    or (mean is not None and (value is None or not math.isclose(value, mean, rel_tol=1e-10)))
                    or row.get("stroke_length_eligible") is not bool(row["eligible"] and valid)):
                raise ValueError("Stroke length window disagrees with complete stroke evidence")
        if stroke is not None:
            for key in ("stroke_count", "stroke_end_count", "stroke_drilling_frame_count"):
                if type(row.get(key)) is not int or row[key] < 0:
                    raise ValueError("Invalid stroke frequency count")
            duration = _number(row.get("stroke_drilling_time_s"))
            frequency, rate = _number(row.get("stroke_frequency_hz")), _number(row.get("stroke_rate_per_s"))
            if (duration is None or not 0 <= duration <= policy["window_s"] + 1e-7
                    or frequency is None or not math.isclose(frequency, row["stroke_end_count"] / policy["window_s"], abs_tol=1e-10)
                    or (duration == 0 and row.get("stroke_rate_per_s") is not None)
                    or (duration > 0 and (rate is None or not math.isclose(rate, row["stroke_count"] / duration, rel_tol=1e-10)))):
                raise ValueError("Stroke frequency disagrees with counts or denominator")
            eligible = bool(row["eligible"] and stroke["source_status"] == "recorded_legacy_segmentation")
            if (row.get("stroke_frequency_eligible") is not eligible
                    or row.get("stroke_rate_eligible") is not bool(eligible and row["stroke_drilling_frame_count"] >= 5 and duration > 0)):
                raise ValueError("Stroke frequency eligibility disagrees with source coverage")


def build_expert_reference(
    window_builds: Sequence[Mapping], *, expert_participant_ids: Sequence[str], provenance: Mapping,
) -> dict:
    """Keep recording windows, enabling self-exclusion before phase-specific weighting."""
    roster = sorted(set(str(value) for value in expert_participant_ids))
    if not roster or any(not value.strip() for value in roster) or not provenance.get("expert_definition"):
        raise ValueError("Reference requires an explicit expert roster and definition")
    builds, seen = [], set()
    protocol = None
    for source in window_builds:
        validate_window_build(source)
        if source.get("participant_id") not in roster:
            raise ValueError("Reference recording participant is not in the explicit expert roster")
        if source["global_run_id"] in seen:
            raise ValueError("Duplicate reference recording")
        seen.add(source["global_run_id"])
        if protocol is not None and protocol != source["protocol"]:
            raise ValueError("Reference recordings use different motion protocols")
        protocol = source["protocol"]
        builds.append(deepcopy(dict(source)))
    if not builds:
        raise ValueError("Cannot build an empty experienced-participant reference")
    result = _seal({
        "schema": REFERENCE_SCHEMA, "protocol": deepcopy(protocol), "protocol_sha256": _digest(protocol),
        "expert_participant_ids": roster, "provenance": deepcopy(dict(provenance)),
        "recordings": builds, "recording_count": len(builds),
        "participant_count": len({b["participant_id"] for b in builds}),
        "weighting": "equal participant, then equal eligible recording per participant and phase/metric, then equal window",
        "self_exclusion": "exclude current recording and all recordings of its known participant before reweighting",
        "percentile": "weighted empirical midrank:100*(weight below +0.5*weight within protocol numeric tie tolerance)",
        "quantile": "left-continuous inverse weighted empirical CDF",
        "claim_boundary": CLAIM_BOUNDARY,
    })
    validate_expert_reference(result)
    return result


def validate_expert_reference(payload: Mapping) -> None:
    if payload.get("schema") != REFERENCE_SCHEMA or payload.get("artifact_sha256") != artifact_sha256(payload):
        raise ValueError("Unsupported expert reference schema or content digest; phase means are not 5s windows")
    protocol = _protocol(payload.get("protocol"))
    if payload.get("protocol_sha256") != _digest(protocol) or not payload.get("provenance", {}).get("expert_definition"):
        raise ValueError("Expert reference lacks its protocol or cohort definition")
    recordings, roster = payload.get("recordings"), payload.get("expert_participant_ids")
    if not isinstance(recordings, list) or not recordings or not isinstance(roster, list) or not roster:
        raise ValueError("Expert reference inventory is empty")
    seen = set()
    for recording in recordings:
        validate_window_build(recording)
        if recording["protocol"] != protocol or recording.get("participant_id") not in roster:
            raise ValueError("Expert reference recording protocol or participant mismatch")
        if recording["global_run_id"] in seen:
            raise ValueError("Duplicate expert reference recording")
        seen.add(recording["global_run_id"])
    if payload.get("recording_count") != len(recordings):
        raise ValueError("Expert reference recording count mismatch")
    if payload.get("participant_count") != len({b["participant_id"] for b in recordings}):
        raise ValueError("Expert reference participant count mismatch")


def _distribution(recordings: list[Mapping], phase: str, metric: str) -> dict:
    groups: dict[str, list[list[float]]] = {}
    eligibility = {"speed_mm_s": "eligible", "removal_rate_mm3_s": "removal_eligible",
                   "mean_stroke_length_mm": "stroke_length_eligible"}[metric]
    for recording in recordings:
        values = [w[metric] for w in recording["windows"]
                  if w["phase"] == phase and w.get(eligibility) and w.get(metric) is not None]
        if values:
            groups.setdefault(recording["participant_id"], []).append(values)
    pairs = sorted((value, 1 / len(groups) / len(records) / len(values))
                   for records in groups.values() for values in records for value in values)
    cumulative = [0.0]
    for _, weight in pairs:
        cumulative.append(cumulative[-1] + weight)
    values = [value for value, _ in pairs]
    total = cumulative[-1]
    quantile = lambda p: values[min(len(values) - 1, max(0, bisect_left(cumulative, p * total) - 1))] if values else None
    return {"values": values, "cumulative": cumulative, "total_weight": total,
            "participant_n": len(groups), "recording_n": sum(map(len, groups.values())),
            "window_n": len(values), "median": quantile(.5), "q10": quantile(.1),
            "q25": quantile(.25), "q75": quantile(.75), "q90": quantile(.9)}


def compare_motion_windows(
    window_build: Mapping, reference: Mapping | None, *, participant_id: str | None = None,
) -> dict:
    validate_window_build(window_build)
    policy = window_build["protocol"]
    known_participant = window_build.get("participant_id")
    if participant_id is not None and known_participant is not None and str(participant_id) != known_participant:
        raise ValueError("Current motion participant identity conflicts with its source")
    known_participant = str(participant_id) if participant_id is not None else known_participant
    result = {
        "schema": COMPARISON_SCHEMA, "global_run_id": window_build["global_run_id"],
        "participant_id": known_participant, "protocol": deepcopy(policy),
        "protocol_sha256": window_build["protocol_sha256"],
        "window_build_sha256": window_build["artifact_sha256"],
        "reference_sha256": None, "available": False, "status": "not_evaluable",
        "reason": "reference_not_configured", "windows": [], "episodes": [], "per_phase": {},
        "reference_summary": {}, "claim_boundary": CLAIM_BOUNDARY,
        "compared_window_count": 0, "candidate_episode_count": 0,
    }
    if reference is None:
        return _seal(result)
    validate_expert_reference(reference)
    if reference["protocol"] != policy:
        raise ValueError("Current and reference motion protocols do not match")
    same_run = [r for r in reference["recordings"] if r["global_run_id"] == window_build["global_run_id"]]
    if same_run:
        roster_identity = same_run[0]["participant_id"]
        if known_participant is not None and known_participant != roster_identity:
            raise ValueError("Current participant conflicts with the reference recording identity")
        known_participant = roster_identity
    excluded = [r for r in reference["recordings"] if r["global_run_id"] == window_build["global_run_id"]
                or (known_participant is not None and r["participant_id"] == known_participant)]
    excluded_ids = {r["global_run_id"] for r in excluded}
    recordings = [r for r in reference["recordings"] if r["global_run_id"] not in excluded_ids]
    result.update(participant_id=known_participant, reference_sha256=reference["artifact_sha256"],
                  reference_summary={
                      "included_recording_n": len(recordings),
                      "included_participant_n": len({r["participant_id"] for r in recordings}),
                      "excluded_recording_ids": [r["global_run_id"] for r in excluded],
                      "self_exclusion_scope": "participant_and_recording" if known_participant else "recording_only_participant_unknown",
                      "reference_phase_sources": sorted({r["provenance"]["phase_source_kind"] for r in recordings}),
                      "query_phase_source_kind": window_build["provenance"]["phase_source_kind"],
                      "weighting": reference["weighting"],
                  })
    metrics = [("speed_mm_s", "eligible"), ("removal_rate_mm3_s", "removal_eligible")]
    length_enabled = any("stroke_length_metadata" in r for r in reference["recordings"])
    if length_enabled:
        metrics.append(("mean_stroke_length_mm", "stroke_length_eligible"))
        result["length_review_policy"] = deepcopy(STROKE_LENGTH_REVIEW_POLICY)
        result["length_episodes"] = []
    distributions = {(phase, metric): _distribution(recordings, phase, metric)
                     for phase in PHASES for metric, _ in metrics}
    for phase in PHASES:
        result["per_phase"][phase] = {metric: {k: v for k, v in distributions[phase, metric].items()
                                                    if k not in ("values", "cumulative", "total_weight")}
                                      for metric, _ in metrics}
    for source in window_build["windows"]:
        row = deepcopy(source)
        row.update(comparisons={}, candidate_low=False, persistent_low=False)
        for metric, eligibility in metrics:
            distribution, value = distributions[row["phase"], metric], row.get(metric)
            comparison = {"observed": value, "status": "not_evaluable", "percentile": None,
                          "relative_deficit": None, "reference_median": distribution["median"],
                          "reference_participant_n": distribution["participant_n"],
                          "reference_recording_n": distribution["recording_n"],
                          "reference_window_n": distribution["window_n"]}
            if metric == "mean_stroke_length_mm":
                comparison["reference_q25"] = distribution["q25"]
            if not row.get(eligibility) or value is None:
                comparison["reason"] = ("no_eligible_complete_strokes_or_insufficient_coverage"
                                        if metric == "mean_stroke_length_mm" else "insufficient_valid_coverage")
            elif distribution["participant_n"] < policy["min_expert_participants"]:
                comparison["reason"] = "insufficient_expert_participants"
            else:
                values, cumulative = distribution["values"], distribution["cumulative"]
                tolerance = policy["percentile_tie_abs_tolerance"] + abs(value) * policy["percentile_tie_rel_tolerance"]
                below, above = bisect_left(values, value - tolerance), bisect_right(values, value + tolerance)
                percentile = 100 * (cumulative[below] + .5 * (cumulative[above] - cumulative[below])) / distribution["total_weight"]
                median = distribution["median"]
                comparison.update(status="compared", reason=None, percentile=percentile,
                                  relative_deficit=(median - value) / median if median > 0 else None)
            row["comparisons"][metric] = comparison
        speed = row["comparisons"]["speed_mm_s"]
        row["candidate_low"] = bool(speed["status"] == "compared" and row["display_clock_valid"]
                                    and speed["percentile"] <= policy["low_percentile"]
                                    and speed["relative_deficit"] is not None
                                    and speed["relative_deficit"] >= policy["min_relative_deficit"])
        if length_enabled:
            measured = row["comparisons"]["mean_stroke_length_mm"]
            row["candidate_short"] = bool(measured["status"] == "compared" and row["display_clock_valid"]
                                           and measured["observed"] < measured["reference_q25"])
            row["persistent_short"] = False
        result["windows"].append(row)
    streak: list[dict] = []

    def finish_streak():
        if len(streak) < policy["persistence_windows"]:
            return
        for window in streak:
            window["persistent_low"] = True
        first, last = streak[0], streak[-1]
        result["episodes"].append({
            "episode_id": f"{window_build['global_run_id']}:motion_review:{len(result['episodes']) + 1:04d}",
            "phase": first["phase"], "phase_name": first["phase_name"], "phase_id": first["phase_id"],
            "t_start_s": first["t_start_s"], "t_end_s": last["t_end_s"],
            "native_start_s": first["native_start_s"], "native_end_s": last["native_end_s"],
            "window_count": len(streak), "source_window_ids": [w["window_id"] for w in streak],
            "minimum_coverage": min(w["coverage_fraction"] for w in streak),
            "maximum_percentile": max(w["comparisons"]["speed_mm_s"]["percentile"] for w in streak),
            "minimum_relative_deficit": min(w["comparisons"]["speed_mm_s"]["relative_deficit"] for w in streak),
            "status": "provisional_descriptive_review_candidate",
        })
    for row in result["windows"]:
        contiguous = (streak and row["phase_id"] == streak[-1]["phase_id"]
                      and row["phase"] == streak[-1]["phase"]
                      and abs(row["native_start_s"] - streak[-1]["native_start_s"] - policy["stride_s"]) < 1e-7)
        if streak and (not row["candidate_low"] or not contiguous):
            finish_streak()
            streak = []
        if row["candidate_low"]:
            streak.append(row)
    finish_streak()
    if length_enabled:
        short: list[dict] = []

        def finish_short():
            if len(short) < STROKE_LENGTH_REVIEW_POLICY["persistence_windows"]:
                return
            for window in short:
                window["persistent_short"] = True
            first, last = short[0], short[-1]
            result["length_episodes"].append({
                "episode_id": f"{window_build['global_run_id']}:stroke_length_review:{len(result['length_episodes']) + 1:04d}",
                "phase": first["phase"], "phase_name": first["phase_name"], "phase_id": first["phase_id"],
                "t_start_s": first["t_start_s"], "t_end_s": last["t_end_s"],
                "native_start_s": first["native_start_s"], "native_end_s": last["native_end_s"],
                "window_count": len(short), "source_window_ids": [w["window_id"] for w in short],
                "minimum_coverage": min(w["coverage_fraction"] for w in short),
                "mean_stroke_length_mm_range": [min(w["mean_stroke_length_mm"] for w in short), max(w["mean_stroke_length_mm"] for w in short)],
                "reference_q25_mm": first["comparisons"]["mean_stroke_length_mm"]["reference_q25"],
                "source_stroke_ids": sorted({sid for w in short for sid in w["stroke_length_ids"]}),
                "status": "provisional_descriptive_review_candidate",
            })
        for row in result["windows"]:
            contiguous = (short and row["phase_id"] == short[-1]["phase_id"] and row["phase"] == short[-1]["phase"]
                          and abs(row["native_start_s"] - short[-1]["native_start_s"] - policy["stride_s"]) < 1e-7)
            if short and (not row["candidate_short"] or not contiguous):
                finish_short()
                short = []
            if row["candidate_short"]:
                short.append(row)
        finish_short()
        result["compared_length_window_count"] = sum(w["comparisons"]["mean_stroke_length_mm"]["status"] == "compared" for w in result["windows"])
        result["length_candidate_episode_count"] = len(result["length_episodes"])
    comparable = sum(w["comparisons"]["speed_mm_s"]["status"] == "compared" for w in result["windows"])
    result.update(available=comparable > 0, status="compared" if comparable else "not_evaluable",
                  reason=None if comparable else "no_eligible_windows_with_sufficient_reference",
                  compared_window_count=comparable, candidate_episode_count=len(result["episodes"]))
    return _seal(result)


def write_motion_artifact(path: Path | str, payload: Mapping) -> None:
    """Persist a versioned artifact; callers choose new result directories."""
    if payload.get("schema") == WINDOW_SCHEMA:
        validate_window_build(payload)
    elif payload.get("schema") == REFERENCE_SCHEMA:
        validate_expert_reference(payload)
    elif payload.get("schema") != COMPARISON_SCHEMA or payload.get("artifact_sha256") != artifact_sha256(payload):
        raise ValueError("Unknown or modified motion artifact")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
