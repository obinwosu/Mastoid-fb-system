"""Deterministic source-to-evidence build without validation experiments."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .events import build_events, write_event_build
from .frame_pipeline import (
    build_strokes,
    compare_legacy_core_frames,
    compare_legacy_strokes,
    compare_legacy_tip_edt,
    compare_legacy_visibility,
    compare_legacy_voxel_edt,
    enrich_removed_voxels_with_edt,
    enrich_tip_edt,
    enrich_visibility,
    extract_core_frames,
    write_core_frame_build,
    write_edt_build,
    write_stroke_build,
    write_tip_edt_build,
    write_visibility_build,
)
from .project import PROJECT_ROOT
from .r4_provenance import assert_geometry_sources_unchanged, validate_production_geometry
from .registry import RunRecord
from .relations import build_relations, compare_legacy_graph, write_relation_build
from .review_video import build_review_video
from .source_audit import build_source_audit, write_source_audit
from .source_config import ResolvedRunSources
from .storage import populate_evidence_store, write_store_manifest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def build_evidence(
    *,
    record: RunRecord,
    sources: ResolvedRunSources,
    result_dir: Path,
    project_root: Path = PROJECT_ROOT,
    build_video: bool = False,
    video_fps: float = 10.0,
) -> dict[str, Any]:
    """Build canonical evidence and a read-only SQLite store for one run."""

    result_dir = result_dir.expanduser().resolve()
    stages = result_dir / "stages"
    geometry_snapshot = validate_production_geometry(record, sources, project_root=project_root)
    audit = build_source_audit(record, sources, hash_hdf5=False)
    audit["geometry_provenance"] = geometry_snapshot.manifest
    if not audit["ready_for_frame_extraction"]:
        raise RuntimeError(
            "Source audit failed: " + ", ".join(audit["blocking_items"])
        )
    write_source_audit(stages / "00_source_audit", audit)

    core = extract_core_frames(record, sources, project_root=project_root)
    if core.manifest.get("hdf5_reader", {}).get("source_schema") == "v3":
        from .frame_pipeline.phase_geometry import validate_v3_volume_contract

        geometry_snapshot.manifest["v3_volume_contract"] = validate_v3_volume_contract(
            core.frames, geometry_snapshot.profile, raw_run_dir=sources.raw_run_dir,
        )
    core.manifest["geometry_provenance"] = geometry_snapshot.manifest
    core_regression = compare_legacy_core_frames(
        core, sources.raw_run_dir / "frame_level_spatial_all_frames_with_voxel.csv"
    )
    write_core_frame_build(stages / "01_core_frames", core, core_regression)

    edt = enrich_removed_voxels_with_edt(
        core, record, sources, project_root=project_root,
        anatomy_profile=geometry_snapshot.profile,
    )
    edt.manifest["geometry_provenance"] = geometry_snapshot.manifest
    edt_regression = compare_legacy_voxel_edt(
        edt, sources.raw_run_dir / "frame_level_spatial_all_frames_voxel_edt.csv"
    )
    write_edt_build(stages / "02_edt_enrichment", edt, edt_regression)

    tip = enrich_tip_edt(
        edt, record, sources, project_root=project_root,
        anatomy_profile=geometry_snapshot.profile,
    )
    tip.manifest["geometry_provenance"] = geometry_snapshot.manifest
    assert_geometry_sources_unchanged(geometry_snapshot)
    tip_regression = compare_legacy_tip_edt(
        tip, sources.raw_run_dir / "frame_level_spatial_all_frames_voxel_edt.csv"
    )
    write_tip_edt_build(stages / "03_tip_edt", tip, tip_regression)

    phase_manifest_path = None
    if sources.phase_model_bundle is not None:
        # Numeric signal features now exist. No manual phase boundary was read.
        from .phase_detection import infer_phases

        phase_dir = stages / "03_phase_detection"
        identity_fields = ("frame_idx_global", "source_hdf5", "source_frame_idx", "t_abs", "t_video_s")
        native_identities = [tuple(row[key] for key in identity_fields) for row in tip.frames]
        original_tip_fields = set(tip.frames[0])
        phase = infer_phases(
            tip.frames, record,
            bundle_dir=sources.phase_model_bundle,
            output_dir=phase_dir,
            geometry_profile=tip.manifest.get("phase_signal_geometry"),
            clock_manifest=core.manifest["clock_alignment"],
        )
        if len(phase.frames) != len(tip.frames):
            raise ValueError("Phase inference changed the native frame inventory")
        for before, after in zip(native_identities, phase.frames):
            if before != tuple(after[key] for key in identity_fields):
                raise ValueError("Phase inference changed source frame identity or clock")
        phase_fields = (set(phase.frames[0])-original_tip_fields) | {"phase", "phase_annotation_id"}
        for core_row, edt_row, predicted in zip(core.frames, edt.frames, phase.frames):
            assigned = {key: predicted[key] for key in phase_fields}
            core_row.update(assigned)
            edt_row.update(assigned)
        tip.frames = phase.frames
        core.phase_intervals = phase.phase_intervals
        core.manifest.update(
            phase_status="completed_model_prediction",
            phase_source_kind="model_phase_prediction",
            first_drilling_s=phase.manifest["first_removal_s"],
            phase_detection=phase.manifest,
        )
        tip.manifest["phase_detection"] = phase.manifest
        edt.manifest["phase_detection"] = phase.manifest
        # Preserve each stage's numerical columns while publishing the same final
        # phase assignments in the frame artifacts consumed by later reviewers.
        write_core_frame_build(stages / "01_core_frames", core, core_regression)
        write_edt_build(stages / "02_edt_enrichment", edt, edt_regression)
        write_tip_edt_build(stages / "03_tip_edt", tip, tip_regression)
        phase_manifest_path = phase_dir / "manifest.json"
        audit["phase_annotation"] = {
            "path": str(phase_manifest_path),
            "sha256": hashlib.sha256(phase_manifest_path.read_bytes()).hexdigest(),
            "valid": True, "row": None, "source_kind": "model_phase_prediction",
            "status": "completed_model_prediction", "human_annotation_read": False,
            "frame_count": len(phase.frames), "manifest": phase.manifest,
        }
        audit["source_classification"]["model_phase_prediction"] = str(phase_manifest_path)
        audit["checks"]["phase_model_predictions_validated"] = True
        write_source_audit(stages / "00_source_audit", audit)

    visibility = enrich_visibility(
        tip, record, sources, project_root=project_root
    )
    visibility.manifest["geometry_provenance"] = geometry_snapshot.manifest
    visibility_regression = compare_legacy_visibility(
        visibility,
        sources.raw_run_dir / "frame_level_spatial_all_frames_visibility.csv",
    )
    write_visibility_build(
        stages / "04_visibility", visibility, visibility_regression
    )

    strokes = build_strokes(visibility, record, project_root=project_root)
    strokes.manifest["geometry_provenance"] = geometry_snapshot.manifest
    stroke_regression = compare_legacy_strokes(strokes, sources.raw_run_dir)
    write_stroke_build(stages / "05_strokes", strokes, stroke_regression)

    # Speed/removal use raw poses; Length reuses complete recorded strokes.
    from .motion_comparison import add_stroke_length, artifact_sha256, build_motion_windows, write_motion_artifact

    motion_path = stages / "09_motion_comparison" / "motion_windows.json"
    motion = build_motion_windows(
        strokes.frames, global_run_id=record.global_run_id,
        participant_id=record.participant_id or None,
        provenance={
            "pose_source": "canonical_vision_pose", "position_unit": "m",
            "phase_source_kind": core.manifest["phase_source_kind"],
            "phase_model_sha256": (_file_sha256(sources.phase_model_bundle / "model.pt")
                                   if sources.phase_model_bundle is not None else None),
            "clock_source": core.manifest["clock_alignment"].get("source"),
            "anatomy_key": record.anatomy_key,
            "source_artifacts": [
                {"kind": kind, "path": str(path), "sha256": _file_sha256(path)}
                for kind, path in (
                    ("frames", stages / "05_strokes" / "frame_observations_complete.csv"),
                    ("phases", stages / "01_core_frames" / "phase_intervals.csv"),
                    ("clock", stages / "01_core_frames" / "extraction_manifest.json"),
                )
            ],
        },
    )
    try:
        motion = add_stroke_length(motion, strokes.frames)
    except ValueError as error:
        # Short/unsegmented recordings can still provide speed/removal evidence.
        motion["stroke_length_unavailable_reason"] = str(error)
        motion["artifact_sha256"] = artifact_sha256(motion)
    write_motion_artifact(motion_path, motion)

    # Exposure coaching consumes this generated sidecar, not the legacy local
    # image-edge candidate. Keep its geometry and clock identical to the frames.
    ray_dir = stages / "08_under_ledge_ray"
    geometry = tip.manifest.get("phase_signal_geometry")
    if geometry is not None:
        from .under_ledge_ray import build_under_ledge_ray, write_ray_build

        ray_dir.mkdir(parents=True, exist_ok=True)
        (ray_dir / "ray_v3_status.json").write_text(
            json.dumps({"run_id": record.run_id, "status": "running"}) + "\n",
            encoding="utf-8",
        )
        try:
            ray = build_under_ledge_ray(
                strokes.frames, strokes.removed_voxels, record, sources,
                project_root=project_root, geometry_profile=geometry,
                anatomy_profile=geometry_snapshot.profile,
            )
        except Exception:
            (ray_dir / "ray_v3_status.json").write_text(
                json.dumps({
                    "run_id": record.run_id, "status": "failed",
                    "reason": "ray_stage_failed", "message": "Exposure screening failed; inspect the build error.",
                }) + "\n", encoding="utf-8",
            )
            raise
        ray.manifest["clock_alignment"] = core.manifest["clock_alignment"]
        ray.manifest["geometry_provenance"] = geometry_snapshot.manifest
        ray.manifest["phase_source_kind"] = core.manifest["phase_source_kind"]
        ray.manifest["input_artifacts"] = {
            name: {"path": str(path), "sha256": _file_sha256(path)}
            for name, path in {
                "frames": stages / "05_strokes" / "frame_observations_complete.csv",
                "removed_voxels": stages / "02_edt_enrichment" / "removed_voxel_edt.csv",
            }.items()
        }
        write_ray_build(ray_dir, ray)
        ray_status = {
            "schema": "under_ledge_stage_status_v1", "run_id": record.run_id,
            "status": "completed", "result_dir": str(ray_dir),
            "candidate_episode_count": ray.manifest["candidate_episode_count"],
            "coaching_eligible_episode_count": ray.manifest["coaching_eligible_episode_count"],
        }
    else:
        ray_dir.mkdir(parents=True, exist_ok=True)
        ray_status = {
            "schema": "under_ledge_stage_status_v1", "run_id": record.run_id,
            "status": "not_run", "reason": "validated_signal_geometry_not_configured",
            "message": "Under-ledge analysis was not run: calibrated geometry is required.",
        }
    (ray_dir / "ray_v3_status.json").write_text(
        json.dumps(ray_status, indent=2) + "\n", encoding="utf-8",
    )

    events = build_events(strokes, core.phase_intervals, record)
    events.manifest["geometry_provenance"] = geometry_snapshot.manifest
    write_event_build(stages / "06_events", events)
    relations = build_relations(events, record)
    relations.manifest["geometry_provenance"] = geometry_snapshot.manifest
    relation_regression = compare_legacy_graph(
        relations, record.golden_reference_path
    )
    write_relation_build(
        stages / "07_relations", relations, relation_regression
    )

    assert_geometry_sources_unchanged(geometry_snapshot)
    database_path = result_dir / "evidence.sqlite3"
    store_manifest = populate_evidence_store(
        database_path,
        record=record,
        sources=sources,
        source_audit=audit,
        core=core,
        strokes=strokes,
        events=events,
        relations=relations,
        artifact_root=stages,
        project_root=project_root,
    )
    write_store_manifest(result_dir, store_manifest)

    video_manifest = None
    if build_video:
        video_manifest = build_review_video(
            record, sources, result_dir, fps=video_fps,
            frame_times_video=[row["t_video_s"] for row in core.frames],
            clock_manifest=core.manifest["clock_alignment"],
        )

    summary = {
        "schema": "evidence_build_summary_v1",
        "dataset_id": record.dataset_id,
        "run_id": record.run_id,
        "global_run_id": record.global_run_id,
        "result_dir": str(result_dir),
        "evidence_database": str(database_path),
        "frame_count": len(strokes.frames),
        "removed_voxel_count": len(strokes.removed_voxels),
        "stroke_count": len(strokes.strokes),
        "event_count": len(events.events),
        "relation_count": len(relations.relations),
        "store_integrity": store_manifest["integrity_check"],
        "phase_source_kind": core.manifest["phase_source_kind"],
        "phase_prediction_manifest": str(phase_manifest_path) if phase_manifest_path else None,
        "under_ledge_detection": ray_status,
        "motion_windows": str(motion_path),
        "review_video": (
            str(result_dir / str(video_manifest["video_file"]))
            if video_manifest else None
        ),
    }
    (result_dir / "build_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8",
    )
    return summary
