"""Public reviewer view for the isolated registration-aware Ray-v3 output."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False, "reason": reason,
        "review_candidates": [],
        "detection_status": {
            "status": "not_configured", "available": False, "reason": reason,
            "message": "Exposure screening is unavailable for this recording; no negative finding can be inferred.",
            "candidate_frame_count": None, "candidate_episode_count": None,
            "evaluable_frame_count": None,
            "high_support_candidate_episode_count": None,
            "coaching_eligible_episode_count": None,
        },
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _candidate_gates(
    episode: dict[str, Any], summary: dict[str, Any], coaching_eligible: bool,
) -> dict[str, Any]:
    """Explain the saved screening decision without reclassifying any episode."""
    config = summary.get("config") or {}
    specifications = (
        ("duration", "Candidate span", "duration_s", "coaching_minimum_episode_duration_s", "s"),
        ("positive_frames", "Supporting frames", "positive_frame_count", "coaching_minimum_positive_frames", "frames"),
        ("mean_blocked_fraction", "Mean blocked fraction", "mean_blocked_contact_fraction", "coaching_minimum_mean_blocked_fraction", "fraction"),
        ("registered_contacts", "Median removal-contact proxies", "median_registered_contact_voxel_count", "coaching_minimum_median_registered_contacts", "voxels"),
    )
    gates = []
    for gate_id, label, metric, threshold_key, unit in specifications:
        value = _number(episode.get(metric))
        threshold = _number(config.get(threshold_key))
        passed = None if value is None or threshold is None else value >= threshold
        if passed is None:
            message = f"{label}: the saved result does not contain enough information to check this criterion."
        elif unit == "fraction":
            message = f"{label}: {100 * value:.1f}% {'meets' if passed else 'is below'} {100 * threshold:g}%."
        else:
            message = f"{label}: {value:.2f} {unit} {'meets' if passed else 'is below'} {threshold:g} {unit}."
        gates.append({
            "gate_id": gate_id, "label": label, "source_metric": metric,
            "observed_value": value, "threshold": threshold,
            "threshold_config_key": threshold_key, "comparison": ">=", "unit": unit,
            "passed": passed, "status": "unknown" if passed is None else "passed" if passed else "failed",
            "message": message,
        })
    audit = summary.get("registration_audit") or {}
    run_value = _number(audit.get("contact_gate_pass_frame_fraction"))
    run_threshold = _number(config.get("coaching_minimum_registration_gate_fraction"))
    gates.append({
        "gate_id": "run_registration", "label": "Recording quality check",
        "source_metric": "registration_audit.contact_gate_pass_frame_fraction",
        "observed_value": run_value, "threshold": run_threshold,
        "threshold_config_key": "coaching_minimum_registration_gate_fraction",
        "comparison": ">=", "unit": "fraction", "passed": coaching_eligible,
        "status": "passed" if coaching_eligible else "failed",
        "decision_source": "saved_run_coaching_eligible_and_evaluable_contacts",
        "reason": summary.get("coaching_suppression_reason"),
        "message": "Recording quality check passed." if coaching_eligible else
                   "Recording quality checks did not support automatic coaching.",
    })
    eligible = coaching_eligible and episode.get("screening_tier") == "high_support_review"
    failures = [gate for gate in gates if gate["passed"] is False]
    unknown = [gate for gate in gates if gate["passed"] is None]
    reasons = [gate["message"] for gate in failures + unknown] if not eligible else []
    if not eligible and not reasons:
        reasons = ["The saved screening result did not qualify this candidate for automatic coaching."]
    return {
        **episode, "category": "under_ledge", "is_ground_truth": False,
        "coaching_eligible": eligible,
        "review_status": "automatic_coaching_eligible" if eligible else "review_only",
        "coaching_gates": gates,
        "failed_coaching_gate_ids": [gate["gate_id"] for gate in failures],
        "unknown_coaching_gate_ids": [gate["gate_id"] for gate in unknown],
        "filter_reasons": reasons,
        "interval_interpretation": "First-to-last positive frame span; may include gaps, not continuous occlusion duration.",
        "claim_boundary": summary.get("claim_boundary") or "Automatic screening candidate; not clinically reviewed ground truth.",
    }


def build_ray_v3_view(
    run_id: str,
    result_dir: Path | None,
    ray_v2_dir: Path | None = None,
) -> dict[str, Any]:
    if result_dir is None:
        return _unavailable("Ray-v3 result directory was not configured.")
    summary_path = result_dir / "ray_v3_summary.json"
    episode_path = result_dir / "ray_v3_candidate_episodes.json"
    status_path = result_dir / "ray_v3_status.json"
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _unavailable("Exposure screening status could not be read.")
        if not isinstance(status, dict) or status.get("run_id") != run_id:
            return _unavailable("Exposure screening status belongs to a different run.")
        if str(status.get("status") or "").lower() in {"not_run", "failed", "running"}:
            # A newer unsuccessful build invalidates old outputs in the same folder.
            result = _unavailable(str(status.get("reason") or "Exposure screening was not run."))
            result["detection_status"]["message"] = str(
                status.get("message") or result["detection_status"]["message"]
            )
            return result
    if not summary_path.is_file() or not episode_path.is_file():
        return _unavailable("Registration-aware Ray-v3 output is not available for this run.")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        episodes = json.loads(episode_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _unavailable("Exposure screening output could not be read.")
    if not isinstance(summary, dict) or not isinstance(episodes, list):
        return _unavailable("Exposure screening output has an unsupported structure.")
    if summary.get("run_id") != run_id:
        return _unavailable("Ray-v3 output belongs to a different run.")
    frame_path = result_dir / "ray_v3_frame_scores.csv"
    frames_by_index: dict[int, dict[str, str]] = {}
    if frame_path.is_file():
        with frame_path.open("r", encoding="utf-8-sig", newline="") as handle:
            frames_by_index = {
                int(row["frame_idx_global"]): row for row in csv.DictReader(handle)
            }
    enriched_episodes = []
    for source_episode in episodes:
        episode = dict(source_episode)
        candidates = [
            frames_by_index[index]
            for index in (int(value) for value in episode.get("source_frame_indices") or [])
            if index in frames_by_index
        ]
        if candidates:
            anchor = max(
                candidates,
                key=lambda row: (
                    float(row.get("blocked_contact_fraction") or 0.0),
                    int(row.get("registered_contact_voxel_count") or 0),
                ),
            )
            blocked_fraction = float(
                anchor.get("blocked_contact_fraction") or 0.0
            )
            registered_count = int(
                anchor.get("registered_contact_voxel_count") or 0
            )
            episode.update({
                "anchor_t_s": float(anchor["t_video_s"]),
                "anchor_frame_idx_global": int(anchor["frame_idx_global"]),
                "anchor_blocked_contact_fraction": blocked_fraction,
                "anchor_registered_contact_voxel_count": registered_count,
                "strength_score": round(blocked_fraction * registered_count, 4),
            })
        enriched_episodes.append(episode)
    episodes = enriched_episodes
    evaluable_count = summary.get("evaluable_frame_count", summary.get("frames_passing_registration_gate"))
    no_evaluable_contacts = evaluable_count is not None and int(evaluable_count) == 0
    coaching_eligible = bool(summary.get("coaching_eligible", True)) and not no_evaluable_contacts
    episodes = [_candidate_gates(row, summary, coaching_eligible) for row in episodes]
    high_support_episodes = [
        row for row in episodes if row.get("screening_tier") == "high_support_review"
    ]
    coaching_episodes = high_support_episodes if coaching_eligible else []
    coaching_example = None
    if coaching_episodes:
        episode = max(
            coaching_episodes,
            key=lambda row: (
                float(row.get("positive_frame_count") or 0)
                * float(row.get("mean_blocked_contact_fraction") or 0),
                -float(row["t_start_s"]),
            ),
        )
        coaching_example = {
            "title": "Improve exposure before drilling deeper",
            "t_start_s": float(episode["t_start_s"]),
            "t_end_s": float(episode["t_end_s"]),
            "phase": episode.get("phase"),
            "delivery_status": "automatic_screening_example",
            "selection_rule": (
                "positive_frame_count_x_mean_blocked_contact_fraction"
            ),
        }
    old_before_150 = None
    if ray_v2_dir is not None:
        frame_path = ray_v2_dir / "ray_v2_frame_scores.csv"
        if frame_path.is_file():
            with frame_path.open("r", encoding="utf-8-sig", newline="") as handle:
                old_before_150 = sum(
                    row.get("ray_state") == "contact_occlusion_candidate"
                    and float(row["t_video_s"]) < 150.0
                    for row in csv.DictReader(handle)
                )
    counts = {
        "frames_with_raw_contact": int(summary.get("frames_with_raw_contact") or 0),
        "frames_passing_registration_gate": int(summary.get("frames_passing_registration_gate") or 0),
        "candidate_frame_count": int(summary.get("candidate_frame_count") or 0),
        "evaluable_frame_count": evaluable_count,
        "candidate_episode_count": len(episodes),
        "high_support_candidate_episode_count": len(high_support_episodes),
        "coaching_eligible_episode_count": len(coaching_episodes),
        "before_150_s_candidate_frame_count": summary.get("before_150_s_candidate_frame_count"),
    }
    reason = summary.get("coaching_suppression_reason")
    if no_evaluable_contacts:
        status = "not_evaluable"
        reason = reason or "no_evaluable_registered_contacts"
        message = "Exposure screening could not assess this recording: no usable contact observations were available."
    elif not coaching_eligible:
        status = "suppressed"
        message = "Exposure screening ran, but its quality checks did not support automatic coaching."
    elif coaching_episodes:
        status = "completed"
        count = len(coaching_episodes)
        message = f"Exposure screening found {count} moment{'s' if count != 1 else ''} for review."
    elif episodes or counts["candidate_frame_count"]:
        status = "low_support"
        message = "Exposure candidates were found, but support was insufficient for automatic coaching."
    else:
        status = "no_candidate"
        message = "Exposure screening found no qualifying candidate; this does not rule out an obstructed view."
    provenance_keys = (
        "schema", "method", "method_lineage", "lineage", "adaptations", "geometry",
        "phase_signal_geometry", "geometry_contract", "geometry_sha256",
        "clock", "clock_alignment", "clock_sha256", "source_hashes",
        "source_manifest_sha256", "input_hashes", "user_review_performed",
        "clinician_review_performed", "phase_source_kind", "input_artifacts",
        "coaching_policy", "geometry_provenance",
    )
    return {
        "available": True,
        "run_id": run_id,
        "anatomy_key": summary.get("anatomy_key"),
        "edt_profile": summary.get("edt_profile"),
        "method": summary.get("method") or "registration_aware_camera_to_contact_ray_v3",
        "provenance": {key: summary[key] for key in provenance_keys if key in summary},
        "detection_status": {
            "status": status, "available": True, "reason": reason,
            "message": message, **counts,
        },
        "registration_orientation": summary.get("registration_orientation"),
        "registration_orientation_selection": (
            summary.get("registration_orientation_selection") or {}
        ),
        "orientation_is_provisional": summary.get("registration_orientation") != "identity",
        "config": summary.get("config") or {},
        "registration_audit": summary.get("registration_audit") or {},
        "counts": counts,
        "comparison": {
            "ray_v2_before_150_s_candidate_frame_count": old_before_150,
        },
        "episodes": episodes,
        "review_candidates": episodes,
        "coaching_episodes": coaching_episodes,
        "coaching_eligible": coaching_eligible,
        "coaching_suppression_reason": summary.get("coaching_suppression_reason"),
        "coaching_example": coaching_example,
        "claim_boundary": summary.get("claim_boundary"),
    }

