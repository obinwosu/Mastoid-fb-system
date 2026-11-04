"""Whole-phase summaries of already verified safety and visibility detections.

This module does not rerun a detector or alter its gates. The caller verifies
source, geometry and clock binding before declaring safety_available. Visibility
duration is a union of qualified first-to-last positive spans, not measured
continuous occlusion. Event counts are distinct qualifying canonical events.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from typing import Any

PHASES = ("early_surface", "antrum", "incus")
STRUCTURE_LABELS = {
    "BonyLabyrinth": "Bony labyrinth",
    "FacialNerve": "Facial nerve",
    "Chorda": "Chorda tympani",
    "CochlearNerve": "Cochlear nerve",
    "IAC": "Internal auditory canal",
    "ICA": "Internal carotid artery",
    "SigSinus": "Sigmoid sinus / dura",
    "SupVestNerve": "Superior vestibular nerve",
    "InfVestNerve": "Inferior vestibular nerve",
    "Stapes": "Stapes",
}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def _phase(value: Any) -> str:
    name = str(value or "")
    return {"early_phase": "early_surface", "pre_antrum": "early_surface",
            "post_incus": "incus"}.get(name, name)


def _bounds(row: Mapping[str, Any]) -> tuple[float, float] | None:
    start, end = _number(row.get("t_start_s")), _number(row.get("t_end_s"))
    return (start, end) if start is not None and end is not None and end >= start else None


def _union(spans: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    return merged


def _group_phases(phases: Sequence[Mapping[str, Any]]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {name: [] for name in PHASES}
    for row in phases:
        name = _phase(row.get("phase_name") or row.get("phase"))
        if name not in grouped:
            continue
        for interval in row.get("intervals", [row]):
            bounds = _bounds(interval)
            if bounds is not None:
                grouped[name].append({"bounds": bounds, "phase_id": interval.get("phase_id")})
    return grouped


def _matches_phase(row: Mapping[str, Any], name: str, intervals: Sequence[Mapping]) -> bool:
    identity = _identity(row)
    known = {interval["phase_id"] for interval in intervals if interval.get("phase_id")}
    if identity and known:
        return identity in known
    declared = _phase(row.get("phase_name") or row.get("phase"))
    return not declared or declared == name


def _identity(row: Mapping[str, Any]) -> Any:
    return row.get("phase_id") or (row.get("attributes") or {}).get("phase_id")


def _point_inside(
    time: float, intervals: Sequence[Mapping], last_time: float, identity: Any = None
) -> bool:
    if identity:
        known = [item for item in intervals if item.get("phase_id") == identity]
        if known:
            return any(item["bounds"][0] <= time <= item["bounds"][1] for item in known)
    return any(
        start <= time < end or time == end == last_time
        for start, end in (item["bounds"] for item in intervals)
    )


def _intersects(
    bounds: tuple[float, float], intervals: Sequence[Mapping], last_time: float,
    identity: Any = None,
) -> bool:
    start, end = bounds
    if start == end:
        return _point_inside(start, intervals, last_time, identity)
    return any(max(start, a) < min(end, b) for a, b in (i["bounds"] for i in intervals))


def _critical_structure(
    name: str,
    intervals: Sequence[Mapping],
    events: Sequence[Mapping[str, Any]],
    frames: Sequence[Mapping[str, Any]],
    available: bool,
    last_time: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": available and bool(intervals),
        "count": None,
        "structures": [],
        "event_ids": [],
        "raw_event_count": None,
        "count_basis": "distinct_coaching_eligible_canonical_events",
        "reference": {"available": False, "reason": "matched_reference_not_available"},
    }
    if not result["available"]:
        result["reason"] = "safety_source_unavailable" if not available else "phase_not_observed"
        return result
    raw_ids, accepted_ids = set(), set()
    structures: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "simulator_overshoot" or not event.get("event_id"):
            continue
        if not _matches_phase(event, name, intervals):
            continue
        bounds = _bounds(event)
        if bounds is None or not _intersects(bounds, intervals, last_time, _identity(event)):
            continue
        event_id = str(event["event_id"])
        raw_ids.add(event_id)
        attributes = event.get("attributes") or {}
        if attributes.get("coaching_eligible", True) is not True:
            continue
        segments = attributes.get("coaching_segments") or [event]
        qualified = [
            segment for segment in segments
            if _matches_phase(segment, name, intervals)
            and _bounds(segment) is not None
            and _intersects(_bounds(segment), intervals, last_time,
                            _identity(segment) or _identity(event))
        ]
        if not qualified:
            continue
        accepted_ids.add(event_id)
        anatomy = str(event.get("anatomy") or "unassigned")
        entry = structures.setdefault(anatomy, {
            "anatomy": anatomy, "name": STRUCTURE_LABELS.get(anatomy, anatomy),
            "event_ids": set(), "depths": [],
        })
        entry["event_ids"].add(event_id)
        # A cross-phase event's global minimum cannot describe either phase.
        # Use only the actual qualifying frame evidence assigned to this phase.
        for frame in frames:
            time = _number(frame.get("t_video_s"))
            if time is None or not _matches_phase(frame, name, intervals):
                continue
            if not _point_inside(time, intervals, last_time, _identity(frame)):
                continue
            if not any(start <= time <= end for start, end in map(_bounds, qualified)):
                continue
            declared = str(frame.get("voxel_safety_signal_structures") or "")
            if "voxel_safety_signal_structures" in frame and anatomy not in declared.split(","):
                continue
            distance = _number(frame.get(f"voxel_min_{anatomy}_mm"))
            if distance is not None and distance <= 0:
                entry["depths"].append(max(0.0, -distance))
    result.update(count=len(accepted_ids), raw_event_count=len(raw_ids),
                  event_ids=sorted(accepted_ids))
    result["structures"] = [
        {"anatomy": anatomy, "name": row["name"], "count": len(row["event_ids"]),
         "event_ids": sorted(row["event_ids"]),
         "max_intrusion_mm": max(row["depths"]) if row["depths"] else None}
        for anatomy, row in sorted(structures.items())
    ]
    return result


def _native_time(time: float, clock: Sequence[tuple[float, float]]) -> float | None:
    """Interpolate only within observed video/native correspondence."""
    index = bisect_left(clock, (time, -math.inf))
    if index < len(clock) and math.isclose(clock[index][0], time, abs_tol=1e-9, rel_tol=0):
        return clock[index][1]
    if index and math.isclose(clock[index - 1][0], time, abs_tol=1e-9, rel_tol=0):
        return clock[index - 1][1]
    if index == 0 or index == len(clock):
        return None
    left, right = clock[index - 1], clock[index]
    fraction = (time - left[0]) / (right[0] - left[0])
    return left[1] + fraction * (right[1] - left[1])


def _visibility(
    name: str, intervals: Sequence[Mapping], ray_view: Mapping[str, Any],
    frames: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    status = (ray_view.get("detection_status") or {}).get("status")
    available = bool(intervals) and ray_view.get("available") is True and (
        ray_view.get("coaching_eligible") is True
        and status not in {"not_evaluable", "suppressed", "unavailable", "failed", "not_run"}
    )
    result: dict[str, Any] = {
        "available": available, "duration_s": None,
        "duration_basis": "union_of_qualified_first_to_last_positive_spans_clipped_to_phase",
        "duration_time_basis": "native_seconds" if frames is not None else "video_seconds",
        "continuous_occlusion_measured": False,
        "reference": {"available": False, "reason": "matched_reference_not_available"},
    }
    if not available:
        result["reason"] = "visibility_source_unavailable" if intervals else "phase_not_observed"
        return result
    spans = []
    for episode in ray_view.get("coaching_episodes") or []:
        if episode.get("coaching_eligible", True) is not True:
            continue
        bounds = _bounds(episode)
        if bounds is None:
            continue
        for a, b in (item["bounds"] for item in intervals):
            start, end = max(a, bounds[0]), min(b, bounds[1])
            if start < end:
                spans.append((start, end))
    merged = _union(spans)
    if frames is None:
        # Explicit compatibility for callers that supply video-time intervals only.
        # The application always supplies bound observations and takes the native path.
        result["duration_s"] = sum(end - start for start, end in merged)
        return result
    clock = []
    for frame in frames:
        video, native = _number(frame.get("t_video_s")), _number(frame.get("t_abs"))
        if video is None or native is None or not _matches_phase(frame, name, intervals):
            continue
        if any(a <= video <= b for a, b in (item["bounds"] for item in intervals)):
            clock.append((video, native))
    clock.sort()
    valid_clock = len(clock) >= 2 and all(
        right[0] > left[0] and right[1] > left[1]
        for left, right in zip(clock, clock[1:], strict=False)
    )
    converted = [(_native_time(start, clock), _native_time(end, clock))
                 for start, end in merged] if valid_clock else []
    if not valid_clock or any(start is None or end is None for start, end in converted):
        result.update(available=False, reason="visibility_clock_unavailable")
        return result
    result["duration_s"] = sum(end - start for start, end in converted)
    return result


def build_phase_safety(
    phases: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    ray_view: Mapping[str, Any],
    *,
    frames: Sequence[Mapping[str, Any]] | None = None,
    safety_available: bool = False,
) -> dict[str, dict[str, Any]]:
    """Aggregate verified detections without mistaking missing evidence for zero."""
    grouped = _group_phases(phases)
    last_time = max((i["bounds"][1] for rows in grouped.values() for i in rows), default=0.0)
    return {
        name: {
            "critical_structure_violation": _critical_structure(
                name, intervals, events, frames or (), safety_available, last_time
            ),
            "drill_visibility": _visibility(name, intervals, ray_view, frames),
        }
        for name, intervals in grouped.items()
    }
