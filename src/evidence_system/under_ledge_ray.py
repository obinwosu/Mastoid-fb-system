"""Calibrated camera-to-contact residual-bone screening, adapted Ray-v3.

The user changed the coaching duration threshold from 0.75 to 0.3 seconds on
2026-11-03; other thresholds retain their original values. R4 calibration replaces
the old orientation rescue and recorder-derived distance scale. This produces
screening evidence, not a clinically validated diagnosis of under-ledge drilling.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .frame_pipeline.edt_enrichment import load_anatomy_profile, read_edt_file
from .frame_pipeline.phase_geometry import MODEL_SCHEMA, resolve_phase_geometry
from .frame_pipeline.tip_edt import _inverse_quaternion_rotate


COACHING_POLICY_VERSION = "ray_v3_coaching_duration_0p3_user_20261103_v1"


@dataclass(frozen=True)
class RayConfig:
    contact_tolerance_mm: float = 1.0
    blocked_fraction_threshold: float = 0.50
    max_contact_voxels_per_frame: int = 20
    samples_per_grid_voxel: float = 2.0
    episode_gap_s: float = 1.0
    minimum_episode_frames: int = 2
    coaching_minimum_episode_duration_s: float = 0.3
    coaching_minimum_positive_frames: int = 5
    coaching_minimum_mean_blocked_fraction: float = 0.70
    coaching_minimum_median_registered_contacts: int = 20
    coaching_minimum_registration_gate_fraction: float = 0.80


@dataclass
class RayBuild:
    rows: list[dict[str, Any]]
    episodes: list[dict[str, Any]]
    manifest: dict[str, Any]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def build_removal_index(removed_voxels, resolution_xyz, frame_times):
    """Use native integer voxels and the same mapped clock as their frame bins."""
    rx, ry, rz = (int(v) for v in resolution_xyz)
    # Float64 preserves exact equality with a frame time at the removal boundary.
    earliest = np.full((rz, ry, rx), np.inf, dtype=np.float64)
    by_frame = defaultdict(list)
    seen_contacts = defaultdict(set)
    counts = {
        "removed_voxel_rows": 0, "removed_voxel_rows_in_bounds": 0,
        "removed_voxel_rows_without_frame": 0,
        "removed_voxel_rows_out_of_bounds": 0,
        "duplicate_frame_contact_rows_ignored": 0,
    }
    previous_time = {}
    previous = None
    for frame_index, frame_time in frame_times.items():
        previous_time[frame_index] = previous
        previous = frame_time
    for row in removed_voxels:
        counts["removed_voxel_rows"] += 1
        values = [_number(row.get(a)) for a in "xyz"]
        if any(value is None or value != int(value) for value in values):
            raise ValueError("Ray screening requires observed integer native voxel coordinates")
        x, native_y, z = (int(value) for value in values)
        y = ry - 1 - native_y
        if not (0 <= x < rx and 0 <= y < ry and 0 <= z < rz):
            counts["removed_voxel_rows_out_of_bounds"] += 1
            continue
        if any(
            key in row and _number(row[key]) != expected
            for key, expected in zip(("edt_x", "edt_y", "edt_z"), (x, y, z), strict=True)
        ):
            raise ValueError("Native removed voxel and stored EDT coordinates disagree")
        time_s = _number(row.get("t_video_s"))
        if time_s is None:
            raise ValueError("Removed voxels require a finite canonical video time")
        earliest[z, y, x] = min(earliest[z, y, x], time_s)
        counts["removed_voxel_rows_in_bounds"] += 1
        raw_index = row.get("frame_idx_global")
        if raw_index is None or str(raw_index).strip() == "":
            counts["removed_voxel_rows_without_frame"] += 1
            continue
        numeric_index = _number(raw_index)
        if numeric_index is None or numeric_index != int(numeric_index):
            raise ValueError("Invalid removed-voxel frame identity")
        frame_index = int(numeric_index)
        if frame_index not in frame_times:
            raise ValueError("Removed voxel refers to an unavailable frame")
        before = previous_time[frame_index]
        if time_s > frame_times[frame_index] or (
            before is not None and time_s <= before
        ):
            raise ValueError("Removed voxel and assigned frame do not share the mapped clock")
        if (x, y, z) in seen_contacts[frame_index]:
            counts["duplicate_frame_contact_rows_ignored"] += 1
            continue
        seen_contacts[frame_index].add((x, y, z))
        by_frame[frame_index].append((x, y, z))
    return dict(by_frame), earliest, counts


def world_to_grid(world_position, volume_position, geometry, resolution_xyz):
    """Use the same fixed R4 pose/translation/scale as the stored tip indices."""
    local = _inverse_quaternion_rotate(
        np.asarray([world_position], float) - np.asarray([volume_position], float),
        np.asarray([geometry["volume_quaternion_xyzw"]], float),
    )[0] / float(geometry["recorder_scale_m_per_simulator_unit"])
    dimensions = np.asarray(geometry["volume_dimensions_sim"], float)
    fraction = local / dimensions + 0.5
    fraction[1] = 0.5 - local[1] / dimensions[1]
    return fraction * np.asarray(resolution_xyz, float)


def _clip_segment(camera, target, resolution_xyz):
    """Clip to outer voxel faces under the inherited nearest-grid-cell sampling."""
    delta = target - camera
    low = np.full(3, -0.5)
    high = np.asarray(resolution_xyz, float) - 0.5
    enter, leave = 0.0, 1.0
    for axis in range(3):
        if abs(delta[axis]) < 1e-15:
            if camera[axis] < low[axis] or camera[axis] > high[axis]:
                return None
            continue
        left = (low[axis] - camera[axis]) / delta[axis]
        right = (high[axis] - camera[axis]) / delta[axis]
        enter = max(enter, min(left, right))
        leave = min(leave, max(left, right))
        if enter > leave:
            return None
    return camera + enter * delta, camera + leave * delta


def ray_residual_bone_evidence(
    camera_grid_xyz, target_grid_xyz, *, time_s, burr_radius_mm,
    initial_bone_zyx, earliest_removal_s_zyx, voxel_spacing_xyz_mm,
    samples_per_grid_voxel=2.0,
):
    """Exclude the terminal burr region; only bone not yet removed may block."""
    camera = np.asarray(camera_grid_xyz, float)
    # Stored plugin indices and native removed-voxel indices address integer
    # voxel centers. Keep that convention at both ends of the ray.
    target = np.asarray(target_grid_xyz, float)
    if not np.isfinite(camera).all() or not np.isfinite(target).all():
        return {"evaluable": False, "blocked": None, "blocker_voxel_count": 0}
    clipped = _clip_segment(camera, target, initial_bone_zyx.shape[::-1])
    if clipped is None:
        return {"evaluable": False, "blocked": None, "blocker_voxel_count": 0}
    start, end = clipped
    count = max(2, int(math.ceil(np.linalg.norm(end-start)*samples_per_grid_voxel)) + 1)
    points = np.linspace(start, end, count)
    cells = np.unique(np.rint(points).astype(int), axis=0)
    resolution = np.asarray(initial_bone_zyx.shape[::-1])
    cells = cells[np.all((cells >= 0) & (cells < resolution), axis=1)]
    distance = np.linalg.norm(
        (cells.astype(float) - target) * np.asarray(voxel_spacing_xyz_mm), axis=1
    )
    cells = cells[distance > float(burr_radius_mm)]
    x, y, z = cells.T
    occupied = initial_bone_zyx[z, y, x] & (
        earliest_removal_s_zyx[z, y, x] > float(time_s)
    )
    return {
        "evaluable": True, "blocked": bool(np.any(occupied)),
        "blocker_voxel_count": int(np.sum(occupied)),
    }


def detect_frame_rows(
    frames, contacts_by_frame, *, initial_bone_zyx, earliest_removal_s_zyx,
    geometry, resolution_xyz, config,
):
    spacing = np.repeat(float(geometry["edt_distance_scale_mm"]), 3)
    rows = []
    for frame in frames:
        index = int(frame["frame_idx_global"])
        contacts = contacts_by_frame.get(index, ())
        time_s = float(frame["t_video_s"])
        radius_value = _number(frame.get("burr_mm"))
        radius = radius_value / 2 if radius_value is not None and radius_value > 0 else None
        tip_values = [_number(frame.get(f"idx_Bone_{a}")) for a in "xyz"]
        tip_valid = all(v is not None for v in tip_values) and all(
            0 <= v < int(r) for v, r in zip(tip_values, resolution_xyz, strict=True)
        )
        trusted = []
        distance_median = None
        if contacts and tip_valid and radius is not None:
            contact_array = np.asarray(contacts, float)
            distances = np.linalg.norm((contact_array-np.asarray(tip_values))*spacing, axis=1)
            distance_median = float(np.median(distances))
            trusted = [
                target for target, distance in zip(contacts, distances, strict=True)
                if distance <= radius+config.contact_tolerance_mm
                and initial_bone_zyx[target[2], target[1], target[0]]
            ]
        camera_values = [_number(frame.get(f"camera_{a}")) for a in "xyz"]
        volume_values = [_number(frame.get(f"volume_{a}")) for a in "xyz"]
        camera_valid = all(v is not None for v in camera_values+volume_values)
        sampled = trusted
        if len(trusted) > config.max_contact_voxels_per_frame:
            positions = np.linspace(
                0, len(trusted)-1, config.max_contact_voxels_per_frame, dtype=int
            )
            sampled = [trusted[int(p)] for p in positions]
        evidence = []
        if sampled and camera_valid:
            camera = world_to_grid(camera_values, volume_values, geometry, resolution_xyz)
            evidence = [ray_residual_bone_evidence(
                camera, target, time_s=time_s, burr_radius_mm=radius,
                initial_bone_zyx=initial_bone_zyx,
                earliest_removal_s_zyx=earliest_removal_s_zyx,
                voxel_spacing_xyz_mm=spacing,
                samples_per_grid_voxel=config.samples_per_grid_voxel,
            ) for target in sampled]
        evaluable = bool(evidence) and all(item["evaluable"] for item in evidence)
        blocked = sum(bool(item["blocked"]) for item in evidence) if evaluable else None
        fraction = blocked / len(evidence) if evaluable else None
        if not contacts:
            state = "no_contact_evidence"
        elif not tip_valid:
            state = "not_evaluable_tip_grid_missing"
        elif radius is None:
            state = "not_evaluable_burr_size_missing"
        elif not trusted:
            state = "not_evaluable_contact_registration_failed"
        elif not camera_valid:
            state = "not_evaluable_camera_pose_missing"
        elif not evaluable:
            state = "not_evaluable_ray_geometry"
        elif fraction >= config.blocked_fraction_threshold:
            state = "contact_occlusion_candidate"
        else:
            state = "clear_line_of_sight_at_threshold"
        rows.append({
            "frame_idx_global": index, "frame_evidence_id": frame.get("frame_evidence_id"),
            "t_video_s": time_s, "phase": frame.get("phase"), "burr_mm": radius_value,
            "raw_contact_voxel_count": len(contacts),
            "registered_contact_voxel_count": len(trusted),
            "uncorrected_contact_tip_distance_median_mm": None,
            "corrected_contact_tip_distance_median_mm": distance_median,
            "sampled_contact_count": len(sampled), "blocked_contact_count": blocked,
            "blocked_contact_fraction": fraction,
            "blocker_voxel_count_sum": sum(v["blocker_voxel_count"] for v in evidence),
            "registration_orientation": "identity", "ray_state": state,
            "evidence_status": "adapted_r4_ray_v3_screening_not_clinically_validated",
        })
    return rows


def group_episodes(rows, config):
    positive = [r for r in rows if r["ray_state"] == "contact_occlusion_candidate"]
    groups = []
    for row in positive:
        if not groups or row["t_video_s"]-groups[-1][-1]["t_video_s"] > config.episode_gap_s:
            groups.append([row])
        else:
            groups[-1].append(row)
    episodes = []
    for group in groups:
        if len(group) < config.minimum_episode_frames:
            continue
        # Normalize only floating subtraction noise (e.g. 100.3 - 100.0).
        # Source endpoints stay unchanged; 0.299 s remains below the 0.3 s gate.
        duration = round(float(group[-1]["t_video_s"]-group[0]["t_video_s"]), 12)
        mean_blocked = float(np.mean([r["blocked_contact_fraction"] for r in group]))
        median_contacts = float(np.median([r["registered_contact_voxel_count"] for r in group]))
        high = (
            duration >= config.coaching_minimum_episode_duration_s
            and len(group) >= config.coaching_minimum_positive_frames
            and mean_blocked >= config.coaching_minimum_mean_blocked_fraction
            and median_contacts >= config.coaching_minimum_median_registered_contacts
        )
        episodes.append({
            "episode_id": f"ray_v3:{len(episodes)+1:04d}",
            "t_start_s": float(group[0]["t_video_s"]),
            "t_end_s": float(group[-1]["t_video_s"]), "duration_s": duration,
            "phase": Counter(str(r.get("phase") or "unknown") for r in group).most_common(1)[0][0],
            "positive_frame_count": len(group), "mean_blocked_contact_fraction": mean_blocked,
            "peak_blocked_contact_fraction": max(r["blocked_contact_fraction"] for r in group),
            "median_registered_contact_voxel_count": median_contacts,
            "source_frame_indices": [r["frame_idx_global"] for r in group],
            "screening_tier": "high_support_review" if high else "low_support_screen",
            "evidence_status": "adapted_r4_ray_v3_screening_not_clinically_validated",
        })
    return episodes


def build_under_ledge_ray(
    frames, removed_voxels, record, sources, *, project_root, geometry_profile=None,
    anatomy_profile=None,
):
    profile = (
        anatomy_profile if anatomy_profile is not None
        else load_anatomy_profile(project_root, record.anatomy_key)
    )
    geometry = resolve_phase_geometry(
        profile, global_run_id=record.global_run_id, edt_profile=record.edt_profile
    )
    if geometry is None:
        raise ValueError("Ray-v3 requires explicit validated R4 or fixture geometry")
    if geometry_profile is not None and geometry_profile != geometry:
        raise ValueError("Ray-v3 profile changed since the calibrated tip was built")
    if not frames:
        raise ValueError("Ray-v3 requires source frame observations")
    for row in (*frames, *removed_voxels):
        if row.get("global_run_id") is not None and row["global_run_id"] != record.global_run_id:
            raise ValueError("Ray-v3 source row belongs to a different run")
    times = [_number(row.get("t_video_s")) for row in frames]
    if any(t is None for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Ray-v3 requires ordered finite canonical frame times")
    indices = [int(row["frame_idx_global"]) for row in frames]
    if len(set(indices)) != len(indices):
        raise ValueError("Ray-v3 frame identities must be unique")
    grid, resolution = read_edt_file(sources.edt_dir / "Bone.edt")
    declared = geometry.get("grid_resolution_xyz")
    if declared is not None and tuple(declared) != tuple(resolution):
        raise ValueError("Ray-v3 EDT resolution differs from validated geometry")
    bone = grid <= 0
    del grid
    contacts, earliest, counts = build_removal_index(
        removed_voxels, resolution, dict(zip(indices, times, strict=True))
    )
    config = RayConfig()
    rows = detect_frame_rows(
        frames, contacts, initial_bone_zyx=bone, earliest_removal_s_zyx=earliest,
        geometry=geometry, resolution_xyz=resolution, config=config,
    )
    episodes = group_episodes(rows, config)
    raw_frames = sum(row["raw_contact_voxel_count"] > 0 for row in rows)
    registered_frames = sum(row["registered_contact_voxel_count"] > 0 for row in rows)
    fraction = registered_frames/max(1, raw_frames)
    eligible = fraction >= config.coaching_minimum_registration_gate_fraction
    high = [episode for episode in episodes if episode["screening_tier"] == "high_support_review"]
    distances = [r["corrected_contact_tip_distance_median_mm"] for r in rows
                 if r["corrected_contact_tip_distance_median_mm"] is not None]
    manifest = {
        "schema": "under_ledge_ray_v3_r4_v1", "lineage": "adapted_r4_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": record.run_id, "global_run_id": record.global_run_id,
        "anatomy_key": record.anatomy_key, "edt_profile": record.edt_profile,
        "config": asdict(config), "geometry": geometry,
        "coaching_policy": {
            "version": COACHING_POLICY_VERSION,
            "change_source": "explicit_user_instruction",
            "change_date": "2026-11-03",
            "changed_threshold": "coaching_minimum_episode_duration_s",
            "previous_value_s": 0.75,
            "current_value_s": config.coaching_minimum_episode_duration_s,
            "other_threshold_values_unchanged": True,
            "episode_duration_decimal_places": 12,
            "interpretation": (
                "User-directed screening policy change, not evidence of improved accuracy "
                "or clinical validation. Prior saved outputs retain their original policy."
            ),
        },
        "distance_metric": {
            "name": "isotropic_R4_EDT_mm",
            "voxel_spacing_xyz_mm": [geometry["edt_distance_scale_mm"]]*3,
            "world_to_grid": (
                ("Fixed source-verified model quaternion, recorded volume translation, "
                 "RecorderScale, ADF dimensions; individual recording configuration assumed")
                if geometry["schema"] == MODEL_SCHEMA else
                "Fixed calibrated quaternion, recorded volume translation, RecorderScale, ADF dimensions"
            ),
            "recorder_scale_used_as_distance_mm": False,
        },
        "clock": {
            "source": "same_canonical_t_video_s_as_frame_pipeline",
            "frame_time_bounds_s": [times[0], times[-1]],
            "assigned_removal_times_validated_against_frame_bins": True,
            "residual_bone_rule": (
                "initial Bone EDT <= 0 and earliest removal time > current frame time"
            ),
        },
        "contact_evidence": (
            "Native removed voxels assigned to a frame; a proxy, "
            "not a separate active-contact sensor"
        ),
        "episode_grouping": (
            "Positive frames separated by at most 1 second, including intervening nonpositive "
            "frames or phase changes; duration is last-positive minus first-positive"
        ),
        "registration_orientation": "identity",
        "registration_orientation_selection": {
            "selected_orientation": "identity",
            "selection_policy": (
                "Current source-verified model configuration; no individual recording calibration or rescue flip"
                if geometry["schema"] == MODEL_SCHEMA else
                "Fixed validated R4 calibration; no legacy Y/Z rescue flip"
            ),
            "selection_uses_under_ledge_labels": False,
            "contact_frame_count": raw_frames,
        },
        "registration_audit": {
            "median_corrected_contact_tip_distance_mm": (
                float(np.median(distances)) if distances else None
            ),
            "contact_gate_pass_frame_fraction": fraction,
        },
        **counts, "frame_count": len(rows), "frames_with_raw_contact": raw_frames,
        "frames_passing_registration_gate": registered_frames,
        "state_counts": dict(Counter(r["ray_state"] for r in rows)),
        "evaluable_frame_count": sum(r["blocked_contact_fraction"] is not None for r in rows),
        "candidate_frame_count": sum(r["ray_state"] == "contact_occlusion_candidate" for r in rows),
        "candidate_episode_count": len(episodes),
        "high_support_candidate_episode_count": len(high),
        "high_support_candidate_frame_count": sum(e["positive_frame_count"] for e in high),
        "coaching_eligible": eligible,
        "coaching_eligible_episode_count": len(high) if eligible else 0,
        "coaching_suppression_reason": (
            "no_contact_evidence" if raw_frames == 0 else
            None if eligible else "run_registration_gate_pass_fraction_below_0.80"
        ),
        "user_review_performed": False, "clinician_review_performed": False,
        "adaptations": [
            "Verified fixed geometry replaces empirical orientation rescue",
            "Explicit R4 EDT distance metric replaces recorder-derived spacing",
            "Rays clipped to grid before inherited nearest-cell sampling",
            "Integer EDT-index centers used consistently; legacy half-voxel target offset removed",
            "Float64 removal clocks preserve exact-time exclusion",
            "Numeric zero tip indices remain valid; unavailable states remain explicit",
            "User-directed coaching duration threshold changed from 0.75 to 0.3 seconds on 2026-11-03",
        ],
        "claim_boundary": (
            "Experimental residual-bone occlusion screening; under-ledge and clinical validity "
            "require review. Absence of a candidate is not evidence of adequate exposure."
        ),
    }
    return RayBuild(rows, episodes, manifest)


def write_ray_build(output_dir, build):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "ray_v3_frame_scores.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(build.rows[0]))
        writer.writeheader()
        writer.writerows(build.rows)
    episode_fields = list(build.episodes[0]) if build.episodes else [
        "episode_id", "t_start_s", "t_end_s", "phase", "positive_frame_count", "screening_tier"
    ]
    episode_path = output_dir / "ray_v3_candidate_episodes.csv"
    with episode_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=episode_fields)
        writer.writeheader()
        writer.writerows(build.episodes)
    for name, payload in (
        ("ray_v3_summary.json", build.manifest),
        ("ray_v3_candidate_episodes.json", build.episodes),
    ):
        (output_dir / name).write_text(
            json.dumps(payload, indent=2, allow_nan=False)+"\n", encoding="utf-8"
        )
