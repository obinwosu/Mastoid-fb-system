"""Browser-observed review intervals; these measure exposure, never attention."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

TIMING_SCHEMA = "review_timing_v1"


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Review timing must contain finite nonnegative numbers")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError("Review timing number is too large") from exc
    if not math.isfinite(result) or result < 0 or result > 8.64e15:
        raise ValueError("Review timing must contain finite nonnegative numbers")
    return result


def validate_snapshot(value: Any, session: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != TIMING_SCHEMA:
        raise ValueError("Invalid review timing snapshot")
    visit_id = value.get("visit_id")
    try:
        if not isinstance(visit_id, str) or str(UUID(visit_id)) != visit_id:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValueError("Review timing visit_id must be a UUID") from exc
    sequence, index = value.get("sequence"), value.get("review_index")
    if type(sequence) is not int or not 1 <= sequence <= 2**53 - 1:
        raise ValueError("Review timing sequence must be a positive integer")
    if type(index) is not int or index not in (0, 1):
        raise ValueError("Invalid review timing index")
    flow = session.get("review_flow") or {}
    order = flow.get("order") or []
    step_id = f"review_{index}"
    if (
        not session.get("analysis_completed_at")
        or value.get("step_id") != step_id
        or len(order) != 2 or value.get("view") != order[index]
    ):
        raise ValueError("Review timing does not match an available review step")
    if flow.get("schema") == "sequential_review_v2":
        reached = {row["step_id"] for row in flow["steps"][:flow["cursor"] + 1]
                   if row["stage"] == "review"}
        if step_id not in reached:
            raise ValueError("Cannot record timing before its review step")
    elif flow.get("schema") != "sequential_review_v1" or index > flow.get("index", -1):
        raise ValueError("Cannot record timing before its review step")
    start = _number(value.get("started_at_ms"))
    end = _number(value.get("observed_until_ms"))
    if end < start:
        raise ValueError("Review timing ends before it starts")
    raw_intervals = value.get("intervals")
    if not isinstance(raw_intervals, list) or len(raw_intervals) > 10000:
        raise ValueError("Review timing has too many intervals")
    intervals, previous_end = [], 0.0
    for row in raw_intervals:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError("Invalid review timing interval")
        first, last = _number(row[0]), _number(row[1])
        foreground, playing = row[2:]
        if (type(foreground) is not bool or type(playing) is not bool
                or (playing and not foreground) or first < previous_end or last <= first
                or last > end - start + 0.01):
            raise ValueError("Invalid or overlapping review timing intervals")
        intervals.append([first, last, foreground, playing])
        previous_end = last
    coverage = value.get("coverage")
    if (not isinstance(coverage, list) or len(coverage) > 2000
            or any(not isinstance(key, str) or not key or len(key) > 256 for key in coverage)):
        raise ValueError("Invalid review coverage")
    closed, reason = value.get("closed"), value.get("end_reason")
    if type(closed) is not bool or reason not in (None, "next", "pagehide", "superseded"):
        raise ValueError("Invalid review timing ending")
    if closed != (reason is not None):
        raise ValueError("Closed review timing requires an ending reason")
    return {
        "schema": TIMING_SCHEMA, "visit_id": visit_id, "step_id": step_id,
        "view": order[index], "review_index": index, "sequence": sequence,
        "started_at_ms": start, "observed_until_ms": end,
        "intervals": intervals, "coverage": sorted(set(coverage)),
        "closed": closed, "end_reason": reason,
    }


def _validate_extension(old: Mapping[str, Any], new: Mapping[str, Any]) -> None:
    for field in ("step_id", "view", "review_index", "started_at_ms"):
        if old[field] != new[field]:
            raise ValueError("A timing visit cannot change identity")
    if new["observed_until_ms"] < old["observed_until_ms"]:
        raise ValueError("A timing visit cannot move backwards")
    if not set(old["coverage"]).issubset(new["coverage"]):
        raise ValueError("A timing snapshot cannot remove saved coverage")
    before, after = old["intervals"], new["intervals"]
    if len(after) < len(before) or (before and before[:-1] != after[:len(before) - 1]):
        raise ValueError("A timing snapshot cannot rewrite saved intervals")
    if before:
        last_old, last_new = before[-1], after[len(before) - 1]
        if (last_old[0] != last_new[0] or last_old[2:] != last_new[2:]
                or last_new[1] < last_old[1]):
            raise ValueError("A timing snapshot cannot rewrite saved intervals")
    if old["closed"] and any(old[key] != new[key] for key in (
        "observed_until_ms", "intervals", "coverage", "closed", "end_reason",
    )):
        raise ValueError("A closed timing visit cannot be reopened")


def _union_ms(intervals: list[tuple[float, float]]) -> float:
    total, right = 0.0, 0.0
    for first, last in sorted(intervals):
        total += max(0.0, last - max(first, right))
        right = max(right, last)
    return total


def aggregate_step(
    visits: list[Mapping[str, Any]], exit_at_ms: float | None = None,
) -> dict[str, Any]:
    start = min(row["started_at_ms"] for row in visits)
    end = max(row["observed_until_ms"] for row in visits)
    if exit_at_ms is not None:
        end = max(start, min(end, exit_at_ms))
    observed, foreground, video = [], [], []
    for visit in visits:
        for first, last, active, playing in visit["intervals"]:
            bounds = (visit["started_at_ms"] + first, min(end, visit["started_at_ms"] + last))
            if bounds[1] <= bounds[0]:
                continue
            observed.append(bounds)
            if active:
                foreground.append(bounds)
            if playing:
                video.append(bounds)
    known = _union_ms(observed)
    return {
        "view": visits[0]["view"], "review_index": visits[0]["review_index"],
        "started_at_ms": start, "last_observed_at_ms": end,
        "review_exit_at_ms": exit_at_ms,
        "elapsed_s": (end - start) / 1000,
        "observed_s": known / 1000,
        "unobserved_s": max(0.0, end - start - known) / 1000,
        "foreground_s": _union_ms(foreground) / 1000,
        "video_playing_s": _union_ms(video) / 1000,
        "overlapping_observation_s": max(
            0.0, sum(last - first for first, last in observed) - known,
        ) / 1000,
        "visit_count": len(visits),
        "coverage": sorted({key for visit in visits for key in visit["coverage"]}),
        "coverage_may_include_post_exit": exit_at_ms is not None and any(
            visit["observed_until_ms"] > exit_at_ms for visit in visits
        ),
    }


def refresh_summaries(session: dict[str, Any]) -> None:
    timing = session.get("review_timing")
    if not timing:
        return
    flow = session["review_flow"]
    for index, view in enumerate(flow["order"]):
        step_id = f"review_{index}"
        visits = [visit for visit in timing["visits"].values() if visit["step_id"] == step_id]
        if not visits:
            continue
        exit_at = next((transition["at"] for transition in flow.get("transitions", [])
                        if transition.get("from_stage") == "review"
                        and transition.get("view") == view), None)
        exit_at_ms = datetime.fromisoformat(exit_at).timestamp() * 1000 if exit_at else None
        timing["steps"][step_id] = aggregate_step(visits, exit_at_ms)


def record_snapshot(session: dict[str, Any], value: Any, received_at: str) -> dict[str, Any]:
    """Called inside the participant transaction; retries replace, never add counters."""
    snapshot = validate_snapshot(value, session)
    timing = session.setdefault(
        "review_timing", {"schema": TIMING_SCHEMA, "visits": {}, "steps": {}},
    )
    visits = timing["visits"]
    old = visits.get(snapshot["visit_id"])
    accepted = old is None or snapshot["sequence"] > old["sequence"]
    if accepted:
        if old:
            _validate_extension(old, snapshot)
        elif len(visits) >= 1024:
            raise ValueError("Too many review timing visits in this recording")
        visits[snapshot["visit_id"]] = {**snapshot, "received_at": received_at}
        refresh_summaries(session)
    saved = visits[snapshot["visit_id"]]
    return {"ok": True, "accepted": accepted, "sequence": saved["sequence"],
            "step": timing["steps"][saved["step_id"]]}
