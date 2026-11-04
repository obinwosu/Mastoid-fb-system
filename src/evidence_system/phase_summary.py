"""Whole-phase motion measurements and portable recording-level expert references.

The phase summaries are recomputed from native rows, never from five-second
averages. Existing source stroke boundaries and their exclusions are preserved.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from .motion_comparison import (
    DEFAULT_PROTOCOL,
    PHASE_NAMES,
    PHASES,
    _number,
    _phase,
    _provenance,
    _seal,
    add_stroke_frequency,
    add_stroke_length,
    artifact_sha256,
    build_motion_windows,
    validate_expert_reference,
)

BUILD_SCHEMA = "phase_motion_v1"
REFERENCE_SCHEMA = "phase_motion_expert_reference_v1"
PROTOCOL = {
    "schema": "whole_phase_motion_protocol_v1",
    "time_basis": "native_elapsed_seconds",
    "pose_source": "canonical_vision_pose",
    "position_unit": "m",
    "max_gap_s": DEFAULT_PROTOCOL["max_gap_s"],
    "min_coverage": DEFAULT_PROTOCOL["min_coverage"],
    "min_expert_participants": DEFAULT_PROTOCOL["min_expert_participants"],
    "phase_interval": "first through last observed row of each contiguous phase identity",
    "repeated_phase": "sum separate intervals, never bridge their intervening phases",
    "speed": "valid within-phase adjacent path mm divided by valid native seconds",
    "removal": "supported frame-assigned removal on (start,end] divided by full phase seconds",
    "stroke_length": "mean eligible complete recorded stroke length, each stroke counted once",
    "stroke_frequency": "recorded endpoint count on (start,end] divided by full phase seconds",
    "tail": "include the entire final partial interval; no fixed-length window grid",
    "stroke_source": "reuse existing finite legacy segmentation; do not repair or resegment",
}
METRICS = {
    "mean_stroke_length_mm": ("Stroke length", "mm"),
    "speed_mm_s": ("Tool speed", "mm/s"),
    "removal_rate_mm3_s": ("Removal rate", "mm³/s"),
    "stroke_frequency_hz": ("Stroke frequency", "strokes/s"),
}
CLAIM_BOUNDARY = (
    "Descriptive whole-phase comparison, not a skill score or a clinical threshold. "
    "Reference phases are manually annotated; current phases may be predicted. "
    "Specimens and assistance conditions are pooled. Source geometry review flags are retained."
)


def _metric(value: float | None, eligible: bool, reason: str | None) -> dict:
    return {"value": value, "eligible": bool(eligible), "reason": None if eligible else reason}


def build_phase_motion(
    frames: Sequence[Mapping[str, Any]],
    *,
    global_run_id: str,
    participant_id: str | None = None,
    provenance: Mapping,
) -> dict:
    """Aggregate full observed phase spans, including tails and repeated intervals.

    We reuse the validated canonical-row and recorded-stroke readers. Their 5s
    windows are not used in any phase arithmetic. Removal at an interval's first
    row is reported separately because it lacks a within-phase predecessor.
    """
    source = build_motion_windows(
        frames, global_run_id=global_run_id, participant_id=participant_id, provenance=provenance
    )
    stroke_keys = ("stroke_id", "stroke_end_flag", "is_drilling_frame")
    stroke_status = "missing_recorded_stroke_fields"
    stroke_error = None
    if all(all(key in row for key in stroke_keys) for row in frames):
        try:
            source = add_stroke_length(add_stroke_frequency(source, frames), frames)
            stroke_status = source["stroke_frequency_metadata"]["source_status"]
        except ValueError as exc:
            stroke_status, stroke_error = "invalid_recorded_stroke_fields", str(exc)

    origin = float(frames[0]["t_abs"])
    native = [float(row["t_abs"]) - origin for row in frames]
    video = [float(row["t_video_s"]) for row in frames]
    identities = []
    derived = None
    for index, frame in enumerate(frames):
        phase, phase_id = _phase(frame), str(frame.get("phase_id") or "")
        if phase is not None and not phase_id:
            if derived is None or not identities or identities[-1][0] != phase:
                derived = f"{global_run_id}:motion_observed_phase:{index:06d}"
            phase_id = derived
        else:
            derived = None
        identities.append((phase, phase_id))

    intervals = {phase: [] for phase in PHASES}
    start = 0
    while start < len(frames):
        end = start + 1
        while end < len(frames) and identities[end] == identities[start]:
            end += 1
        phase, phase_id = identities[start]
        if phase is not None:
            intervals[phase].append(
                {
                    "phase_id": phase_id,
                    "source_start_row": start,
                    "source_end_row": end - 1,
                    "native_start_s": native[start],
                    "native_end_s": native[end - 1],
                    "t_start_s": video[start],
                    "t_end_s": video[end - 1],
                    "duration_s": native[end - 1] - native[start],
                }
            )
        start = end

    summaries = []
    for phase in PHASES:
        spans = intervals[phase]
        if not spans:
            continue
        duration = sum(span["duration_s"] for span in spans)
        path = valid = removed = removal_valid = gap_removal = boundary_removal = 0.0
        endpoints = 0
        display_valid = True
        selected_strokes = []
        for span in spans:
            first, last = span["source_start_row"], span["source_end_row"]
            boundary_removal += _number(frames[first]["frame_removed_mm3"]) or 0.0
            for index in range(first + 1, last + 1):
                dt = native[index] - native[index - 1]
                short = dt <= PROTOCOL["max_gap_s"] + 1e-9
                display_valid &= (
                    video[index] - video[index - 1]
                    <= DEFAULT_PROTOCOL["max_display_clock_step_s"] + 1e-9
                )
                previous = [
                    _number(frames[index - 1][key]) for key in ("drill_x", "drill_y", "drill_z")
                ]
                current = [_number(frames[index][key]) for key in ("drill_x", "drill_y", "drill_z")]
                if short and all(value is not None for value in previous + current):
                    path += math.dist(previous, current) * 1000
                    valid += dt
                removal = _number(frames[index]["frame_removed_mm3"])
                if short and removal is not None:
                    removed += removal
                    removal_valid += dt
                elif removal is not None:
                    gap_removal += removal
                if "stroke_frequency_metadata" in source:
                    flag = frames[index]["stroke_end_flag"]
                    endpoints += flag is True or str(flag).casefold() in {"true", "1", "1.0"}
            selected_strokes.extend(
                stroke
                for stroke in source.get("stroke_lengths", [])
                if stroke["eligible"]
                and stroke["phase"] == phase
                and stroke["phase_id"] == span["phase_id"]
                and first <= stroke["source_start_row"] <= stroke["source_end_row"] <= last
            )
        coverage = min(1.0, valid / duration) if duration > 0 else 0.0
        removal_coverage = min(1.0, removal_valid / duration) if duration > 0 else 0.0
        pose_eligible = duration > 0 and coverage + 1e-9 >= PROTOCOL["min_coverage"]
        removal_eligible = duration > 0 and removal_coverage + 1e-9 >= PROTOCOL["min_coverage"]
        stroke_eligible = pose_eligible and stroke_status == "recorded_legacy_segmentation"
        length = (
            sum(stroke["length_mm"] for stroke in selected_strokes) / len(selected_strokes)
            if selected_strokes
            else None
        )
        stroke_reason = (
            "insufficient_valid_coverage"
            if not pose_eligible
            else stroke_status
            if stroke_status != "recorded_legacy_segmentation"
            else "no_eligible_complete_strokes"
        )
        summaries.append(
            {
                "phase": phase,
                "phase_name": PHASE_NAMES[phase],
                "intervals": spans,
                "duration_s": duration,
                "valid_duration_s": valid,
                "path_length_mm": path,
                "coverage_fraction": coverage,
                "removed_mm3": removed,
                "removal_valid_duration_s": removal_valid,
                "removal_coverage_fraction": removal_coverage,
                "excluded_gap_removal_mm3": gap_removal,
                "excluded_boundary_removal_mm3": boundary_removal,
                "stroke_end_count": endpoints if "stroke_frequency_metadata" in source else None,
                "eligible_stroke_count": len(selected_strokes),
                "eligible_stroke_ids": [stroke["stroke_id"] for stroke in selected_strokes],
                "display_clock_valid": bool(display_valid and duration > 0),
                "metrics": {
                    "speed_mm_s": _metric(
                        path / valid if valid > 0 else None,
                        pose_eligible,
                        "insufficient_valid_coverage",
                    ),
                    "removal_rate_mm3_s": _metric(
                        removed / duration if duration > 0 else None,
                        removal_eligible,
                        "insufficient_removal_coverage",
                    ),
                    "mean_stroke_length_mm": _metric(
                        length, stroke_eligible and length is not None, stroke_reason
                    ),
                    "stroke_frequency_hz": _metric(
                        endpoints / duration
                        if duration > 0 and "stroke_frequency_metadata" in source
                        else None,
                        stroke_eligible,
                        stroke_reason,
                    ),
                },
            }
        )
    return _seal(
        {
            "schema": BUILD_SCHEMA,
            "global_run_id": str(global_run_id),
            "participant_id": str(participant_id) if participant_id is not None else None,
            "protocol": deepcopy(PROTOCOL),
            "provenance": deepcopy(source["provenance"]),
            "input_rows_sha256": source["input_rows_sha256"],
            "source_frame_count": len(frames),
            "native_time_origin_abs": origin,
            "native_duration_s": native[-1],
            "stroke_source_status": stroke_status,
            "stroke_source_error": stroke_error,
            "stroke_frequency_metadata": {
                key: deepcopy(value)
                for key, value in source.get("stroke_frequency_metadata", {}).items()
                if key != "contract"
            },
            "stroke_length_metadata": {
                key: deepcopy(value)
                for key, value in source.get("stroke_length_metadata", {}).items()
                if key != "contract"
            },
            "phases": summaries,
            "claim_boundary": CLAIM_BOUNDARY,
        }
    )


def validate_phase_motion(payload: Mapping) -> None:
    if payload.get("schema") != BUILD_SCHEMA or payload.get("artifact_sha256") != artifact_sha256(
        payload
    ):
        raise ValueError("Invalid whole-phase motion schema or content digest")
    if payload.get("protocol") != PROTOCOL or not payload.get("global_run_id"):
        raise ValueError("Whole-phase motion protocol or identity differs")
    _provenance(payload["provenance"])
    if not isinstance(payload.get("phases"), list):
        raise ValueError("Whole-phase inventory is missing")
    seen = set()
    for phase in payload.get("phases", []):
        if phase.get("phase") not in PHASES or phase["phase"] in seen:
            raise ValueError("Whole-phase inventory has an unknown or duplicate phase")
        seen.add(phase["phase"])
        duration = _number(phase.get("duration_s"))
        if (
            duration is None
            or duration < 0
            or not math.isclose(duration, sum(span["duration_s"] for span in phase["intervals"]))
        ):
            raise ValueError("Whole-phase duration disagrees with its observed intervals")
        previous_end = -1
        for span in phase["intervals"]:
            if (
                span["source_start_row"] <= previous_end
                or span["source_end_row"] < span["source_start_row"]
                or span["source_end_row"] >= payload["source_frame_count"]
                or span["t_end_s"] < span["t_start_s"]
                or span["native_end_s"] < span["native_start_s"]
                or not math.isclose(
                    span["duration_s"], span["native_end_s"] - span["native_start_s"]
                )
            ):
                raise ValueError("Whole-phase source intervals are inconsistent")
            previous_end = span["source_end_row"]
        if set(phase.get("metrics", {})) != set(METRICS):
            raise ValueError("Whole-phase metric inventory differs")
        for metric in phase["metrics"].values():
            value = metric.get("value")
            if (value is not None and (_number(value) is None or value < 0)) or (
                metric.get("eligible") and value is None
            ):
                raise ValueError("Whole-phase metric is invalid")
        valid, removal_valid = phase["valid_duration_s"], phase["removal_valid_duration_s"]
        coverage = min(1.0, valid / duration) if duration > 0 else 0.0
        removal_coverage = min(1.0, removal_valid / duration) if duration > 0 else 0.0
        if (
            not 0 <= valid <= duration + 1e-9
            or not 0 <= removal_valid <= duration + 1e-9
            or not math.isclose(phase["coverage_fraction"], coverage)
            or not math.isclose(phase["removal_coverage_fraction"], removal_coverage)
            or phase["eligible_stroke_count"] != len(set(phase["eligible_stroke_ids"]))
        ):
            raise ValueError("Whole-phase coverage or stroke counts are inconsistent")
        expected = {
            "speed_mm_s": phase["path_length_mm"] / valid if valid > 0 else None,
            "removal_rate_mm3_s": phase["removed_mm3"] / duration if duration > 0 else None,
            "stroke_frequency_hz": phase["stroke_end_count"] / duration
            if duration > 0 and phase["stroke_end_count"] is not None
            else None,
        }
        for key, value in expected.items():
            observed = phase["metrics"][key]["value"]
            if (value is None) != (observed is None) or (
                value is not None and not math.isclose(value, observed, abs_tol=1e-12)
            ):
                raise ValueError("Whole-phase metric disagrees with its numerator or denominator")
        pose_good = duration > 0 and coverage + 1e-9 >= PROTOCOL["min_coverage"]
        stroke_good = (
            pose_good and payload["stroke_source_status"] == "recorded_legacy_segmentation"
        )
        eligibility = {
            "speed_mm_s": pose_good,
            "removal_rate_mm3_s": duration > 0
            and removal_coverage + 1e-9 >= PROTOCOL["min_coverage"],
            "stroke_frequency_hz": stroke_good,
            "mean_stroke_length_mm": stroke_good and phase["eligible_stroke_count"] > 0,
        }
        if any(phase["metrics"][key]["eligible"] != value for key, value in eligibility.items()):
            raise ValueError("Whole-phase eligibility disagrees with source quality")


def build_phase_expert_reference(
    builds: Sequence[Mapping], *, expert_participant_ids: Sequence[str], provenance: Mapping
) -> dict:
    roster = sorted(set(expert_participant_ids))
    if not builds or not roster or not provenance.get("expert_definition"):
        raise ValueError(
            "Whole-phase reference requires recordings and an explicit expert definition"
        )
    result = _seal(
        {
            "schema": REFERENCE_SCHEMA,
            "protocol": deepcopy(PROTOCOL),
            "expert_participant_ids": roster,
            "provenance": deepcopy(dict(provenance)),
            "recordings": deepcopy(list(builds)),
            "recording_count": len(builds),
            "participant_count": len({build["participant_id"] for build in builds}),
            "weighting": (
                "equal participant, then equal eligible recording per participant and phase/metric"
            ),
            "quantile": "left-continuous inverse weighted empirical CDF",
            "self_exclusion": (
                "exclude current recording and all recordings of its known participant "
                "before weighting"
            ),
            "claim_boundary": CLAIM_BOUNDARY,
        }
    )
    validate_phase_reference(result)
    return result


def validate_phase_reference(payload: Mapping) -> None:
    if payload.get("schema") != REFERENCE_SCHEMA or payload.get(
        "artifact_sha256"
    ) != artifact_sha256(payload):
        raise ValueError("Invalid whole-phase expert reference schema or content digest")
    if payload.get("protocol") != PROTOCOL or not payload.get("provenance", {}).get(
        "expert_definition"
    ):
        raise ValueError("Whole-phase reference lacks its protocol or expert definition")
    recordings, roster = payload.get("recordings"), payload.get("expert_participant_ids")
    if (
        not isinstance(recordings, list)
        or not recordings
        or not isinstance(roster, list)
        or not roster
    ):
        raise ValueError("Whole-phase reference inventory is empty")
    seen = set()
    for recording in recordings:
        validate_phase_motion(recording)
        if recording.get("participant_id") not in roster or recording["global_run_id"] in seen:
            raise ValueError(
                "Whole-phase reference has a duplicate recording or unknown participant"
            )
        seen.add(recording["global_run_id"])
    if payload.get("recording_count") != len(recordings) or payload.get("participant_count") != len(
        {recording["participant_id"] for recording in recordings}
    ):
        raise ValueError("Whole-phase reference inventory counts disagree")


def load_phase_reference(path: Path) -> dict:
    """Read the saved reference without opening any archived expert source path."""
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    validate_phase_reference(payload)
    return payload


def _reference_distribution(recordings: Sequence[Mapping], phase: str, metric: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for recording in recordings:
        row = next((row for row in recording["phases"] if row["phase"] == phase), None)
        if row is not None and row["metrics"][metric]["eligible"]:
            groups.setdefault(recording["participant_id"], []).append(
                {
                    "value": row["metrics"][metric]["value"],
                    "recording_id": recording["provenance"].get(
                        "recording_id", recording["global_run_id"]
                    ),
                    "participant_id": recording["participant_id"],
                }
            )
    points = sorted(
        (
            {**point, "weight": 1 / len(groups) / len(group)}
            for group in groups.values()
            for point in group
        ),
        key=lambda point: (point["value"], point["recording_id"]),
    )

    def quantile(fraction: float):
        total = 0.0
        for point in points:
            total += point["weight"]
            if total + 1e-12 >= fraction:
                return point["value"]
        return points[-1]["value"] if points else None

    available = len(groups) >= PROTOCOL["min_expert_participants"]
    return {
        "available": available,
        "reason": None if available else "insufficient_expert_participants",
        "points": points,
        "participant_n": len(groups),
        "recording_n": len(points),
        "q25": quantile(0.25),
        "median": quantile(0.5),
        "q75": quantile(0.75),
    }


def compare_phase_motion(
    build: Mapping, reference: Mapping | None, *, participant_id: str | None = None
) -> dict:
    """Attach same-phase expert distributions, with no coaching or ranking threshold."""
    validate_phase_motion(build)
    known = build.get("participant_id")
    if participant_id is not None and known is not None and participant_id != known:
        raise ValueError("Current phase-motion participant conflicts with its source")
    known = participant_id if participant_id is not None else known
    recordings, excluded = [], []
    if reference is not None:
        validate_phase_reference(reference)
        current_id = build["provenance"].get("recording_id")

        def same_recording(recording):
            return recording["global_run_id"] == build["global_run_id"] or (
                current_id is not None and recording["provenance"].get("recording_id") == current_id
            )

        for recording in reference["recordings"]:
            if same_recording(recording):
                if known is not None and known != recording["participant_id"]:
                    raise ValueError("Current participant conflicts with the reference recording")
                known = recording["participant_id"]
        for recording in reference["recordings"]:
            if same_recording(recording) or (
                known is not None and recording["participant_id"] == known
            ):
                excluded.append(recording["global_run_id"])
            else:
                recordings.append(recording)
    phases = []
    for phase in build["phases"]:
        metrics = {}
        for key, (label, unit) in METRICS.items():
            observed = phase["metrics"][key]
            distribution = _reference_distribution(recordings, phase["phase"], key)
            if reference is None:
                distribution["reason"] = "reference_not_configured"
            metrics[key] = {
                "key": key,
                "label": label,
                "unit": unit,
                "value": observed["value"],
                "available": observed["eligible"],
                "reason": observed["reason"],
                "reference": distribution,
            }
        phases.append({**deepcopy(phase), "metrics": metrics})
    return {
        "schema": "phase_motion_comparison_v1",
        "global_run_id": build["global_run_id"],
        "participant_id": known,
        "phases": phases,
        "reference_sha256": reference["artifact_sha256"] if reference is not None else None,
        "reference_summary": {
            "recording_count": len(recordings),
            "participant_count": len({recording["participant_id"] for recording in recordings}),
            "excluded_recording_ids": excluded,
            "self_exclusion_scope": "participant_and_recording"
            if known
            else "recording_only_participant_unknown",
        },
        "claim_boundary": CLAIM_BOUNDARY,
    }


def build_phase_reference_from_motion_reference(source_path: Path, output: Path) -> dict:
    """Re-read frozen R4 source rows for exactly the existing reference cohort.

    The archived window artifact is used only for source identities/hashes and
    the expert roster, never as the numerical input to whole-phase means.
    """
    source_path, output = Path(source_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Use a new reference filename; existing references are preserved")
    raw = source_path.read_bytes()
    reference = json.loads(raw)
    validate_expert_reference(reference)
    if (
        reference["provenance"].get("release") != "R4_20261029"
        or reference["provenance"].get("release_manifest_sha256")
        != "99fc284bef5e3cbf843be99da1ce5678df735a20257adbf89f02103f04304e7a"
    ):
        raise ValueError("Whole-phase reference generation requires the frozen R4 source cohort")
    for source in reference["provenance"].get("source_artifacts", []):
        if output.is_relative_to(Path(source["path"]).resolve().parent):
            raise ValueError("Reference output must remain outside the append-only source catalog")

    def verified_rows(source):
        path = Path(source["path"])
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != source["sha256"]:
            raise ValueError(f"Frozen reference source changed: {path}")
        return list(csv.DictReader(io.StringIO(contents.decode("utf-8-sig"), newline="")))

    builds = []
    for recording in reference["recordings"]:
        sources = {item["kind"]: item for item in recording["provenance"]["source_artifacts"]}
        frames, aligned = verified_rows(sources["frames"]), verified_rows(sources["phases"])
        if len(frames) != len(aligned) or len(frames) != recording["source_frame_count"]:
            raise ValueError("Frozen reference row inventories differ")
        first_removal = next(
            (i for i, row in enumerate(frames) if float(row["frame_removed_voxels"] or 0) > 0),
            len(frames),
        )
        origin = float(frames[0]["t_abs"])
        previous, interval = None, 0
        for index, (row, alignment) in enumerate(zip(frames, aligned, strict=True)):
            if int(row["frame_idx_global"]) != int(alignment["frame_idx_global"]):
                raise ValueError("Frozen reference phase row identities differ")
            phase = (
                "pre_drilling"
                if index < first_removal
                else {
                    "pre_antrum": "early_surface",
                    "antrum": "antrum",
                    "incus": "incus",
                    "post_incus": "incus",
                }.get(alignment["phase"])
            )
            if phase is None:
                raise ValueError("Frozen reference contains an unsupported phase")
            if phase != previous:
                interval += 1
            row.update(
                t_video_s=float(row["t_abs"]) - origin,
                phase=phase,
                phase_id=f"{recording['global_run_id']}:phase:{interval}",
            )
            previous = phase
        build = build_phase_motion(
            frames,
            global_run_id=recording["global_run_id"],
            participant_id=recording["participant_id"],
            provenance=recording["provenance"],
        )
        if build["input_rows_sha256"] != recording["input_rows_sha256"]:
            raise ValueError("Whole-phase source transformation differs from the verified R4 rows")
        builds.append(build)
    result = build_phase_expert_reference(
        builds,
        expert_participant_ids=reference["expert_participant_ids"],
        provenance={
            **deepcopy(reference["provenance"]),
            "cohort_source": {
                "path": str(source_path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "artifact_sha256": reference["artifact_sha256"],
            },
            "aggregation_source": (
                "verified original frame and alignment rows, not five-second summaries"
            ),
        },
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    return {
        "output": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "recording_count": len(builds),
        "participant_count": result["participant_count"],
    }
