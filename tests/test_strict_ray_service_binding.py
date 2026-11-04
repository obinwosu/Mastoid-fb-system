"""Serve-boundary rejection of stale or foreign Ray sidecars for strict builds."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import test_ray_feedback_runtime as runtime_fixtures


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def strict_service(tmp_path):
    root = tmp_path / "recording"
    service = runtime_fixtures.make_service(root)
    stages = root / "stages"
    ray_dir = stages / "08_under_ledge_ray"
    runtime_fixtures.write_ray(ray_dir)
    authority = {
        "schema": "production_geometry_provenance_v1",
        "status": "verified_R4_v3_sources",
        "real_R4_validated": True,
        "global_run_id": "synthetic:case",
        "actual_specimen": "generated",
        "source_files": {"correction_table": {"sha256": "a" * 64}},
        "geometry_quality": "needs_review",
        "clinician_review_performed": False,
    }
    alignment = root / "alignment.csv"
    alignment.write_text("frame_idx_global,video_time_s\n0,0\n1,1\n")
    clock = {
        "schema": "frame_clock_alignment_v1",
        "configured_global_run_id": "synthetic:case",
        "source": "explicit_frame_alignment",
        "path": str(alignment.resolve()),
        "sha256": digest(alignment),
        "frame_count": 2,
        "video_time_bounds_s": [0, 1],
    }
    paths = {
        "frames": stages / "05_strokes/frame_observations_complete.csv",
        "removed_voxels": stages / "02_edt_enrichment/removed_voxel_edt.csv",
        "strokes": stages / "05_strokes/strokes.csv",
    }
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"generated_test_field\n{name}\n")
    core = stages / "01_core_frames/extraction_manifest.json"
    core.parent.mkdir(parents=True)
    core.write_text(
        json.dumps(
            {
                "global_run_id": "synthetic:case",
                "geometry_provenance": authority,
                "clock_alignment": clock,
            }
        )
    )
    phase = stages / "03_phase_detection/manifest.json"
    phase.parent.mkdir(parents=True)
    phase.write_text(json.dumps({"global_run_id": "synthetic:case", "clock": clock}))
    audit = {"global_run_id": "synthetic:case", "geometry_provenance": authority}
    audit_path = stages / "00_source_audit/source_manifest.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(json.dumps(audit))
    canonical = json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(root / "evidence.sqlite3") as connection:
        connection.execute(
            "UPDATE runs SET source_manifest_hash = ?",
            (hashlib.sha256(canonical.encode()).hexdigest(),),
        )
        connection.execute(
            "INSERT INTO generated_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test:stroke",
                "synthetic:case",
                "stroke_build",
                str(paths["strokes"].resolve()),
                digest(paths["strokes"]),
                "test",
                "test",
                "test",
                "test",
            ),
        )
        connection.execute(
            "INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test:phase",
                "synthetic:case",
                "model_phase_prediction",
                str(phase.resolve()),
                digest(phase),
                phase.stat().st_size,
                "test",
                1,
            ),
        )
    summary_path = ray_dir / "ray_v3_summary.json"
    summary = json.loads(summary_path.read_text())
    summary.update(
        geometry_provenance=authority,
        clock_alignment=clock,
        input_artifacts={
            name: {"path": str(paths[name].resolve()), "sha256": digest(paths[name])}
            for name in ("frames", "removed_voxels")
        },
    )
    summary_path.write_text(json.dumps(summary))
    return service, root, ray_dir


def test_strict_generated_sidecar_and_identical_external_copy_are_accepted(
    strict_service, tmp_path
):
    service, _, ray_dir = strict_service
    first = service.bootstrap()
    assert first["under_ledge_screening"]["binding_validation"]["status"] == "verified"
    assert len(first["coaching_plan"]["items"]) == 1
    override = tmp_path / "explicit_ray_copy"
    shutil.copytree(ray_dir, override)
    service.ray_v3_dir = override
    copied = service.bootstrap()
    assert copied["detection_status"]["under_ledge"]["source"] == "explicit"
    assert copied["under_ledge_screening"]["binding_validation"]["status"] == "verified"
    assert copied["coaching_plan"] == first["coaching_plan"]


@pytest.mark.parametrize(
    "attack",
    [
        "old_sidecar",
        "wrong_geometry",
        "same_run_wrong_clock",
        "wrong_input_hash",
        "wrong_input_path",
        "changed_stage_input",
        "changed_authority",
        "missing_authority",
        "changed_alignment",
        "changed_phase_manifest",
    ],
)
def test_strict_db_rejects_unbound_same_run_sidecar_before_coaching(
    strict_service, tmp_path, attack
):
    service, root, ray_dir = strict_service
    # Validate once first, so source-change cases also test hash-cache invalidation.
    assert len(service.bootstrap()["coaching_plan"]["items"]) == 1
    override = tmp_path / "unbound_override"
    shutil.copytree(ray_dir, override)
    service.ray_v3_dir = override
    path = override / "ray_v3_summary.json"
    summary = json.loads(path.read_text())
    if attack == "old_sidecar":
        del summary["geometry_provenance"]
    elif attack == "wrong_geometry":
        summary["geometry_provenance"]["actual_specimen"] = "other_generated_specimen"
    elif attack == "same_run_wrong_clock":
        summary["clock_alignment"]["video_time_bounds_s"] = [20, 21]
    elif attack == "wrong_input_hash":
        summary["input_artifacts"]["frames"]["sha256"] = "0" * 64
    elif attack == "wrong_input_path":
        summary["input_artifacts"]["frames"]["path"] = str(override / "frames.csv")
    elif attack == "changed_stage_input":
        Path(summary["input_artifacts"]["removed_voxels"]["path"]).write_text("changed\n")
    elif attack == "changed_authority":
        audit = root / "stages/00_source_audit/source_manifest.json"
        value = json.loads(audit.read_text())
        value["geometry_provenance"]["actual_specimen"] = "changed"
        audit.write_text(json.dumps(value))
    elif attack == "missing_authority":
        (root / "stages/00_source_audit/source_manifest.json").unlink()
    elif attack == "changed_alignment":
        (root / "alignment.csv").write_text("changed\n")
    elif attack == "changed_phase_manifest":
        (root / "stages/03_phase_detection/manifest.json").write_text("{}")
    path.write_text(json.dumps(summary))
    result = service.bootstrap()
    status = result["detection_status"]["under_ledge"]
    assert status["status"] == "unavailable" and not status["available"]
    assert status["reason"] == "strict_ray_binding_mismatch"
    assert result["review_candidates"] == []
    assert result["coaching_plan"]["items"] == []


def rebind_generated_fixture(root, ray_dir, authority, clock):
    """Update all fixture authority hashes to exercise checks inside the binding."""
    stages = root / "stages"
    audit_path = stages / "00_source_audit/source_manifest.json"
    audit = json.loads(audit_path.read_text())
    audit["geometry_provenance"] = authority
    audit_path.write_text(json.dumps(audit))
    canonical = json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    core_path = stages / "01_core_frames/extraction_manifest.json"
    core = json.loads(core_path.read_text())
    core.update(geometry_provenance=authority, clock_alignment=clock)
    core_path.write_text(json.dumps(core))
    phase_path = stages / "03_phase_detection/manifest.json"
    phase = json.loads(phase_path.read_text())
    phase["clock"] = clock
    phase_path.write_text(json.dumps(phase))
    summary_path = ray_dir / "ray_v3_summary.json"
    summary = json.loads(summary_path.read_text())
    summary.update(geometry_provenance=authority, clock_alignment=clock)
    summary_path.write_text(json.dumps(summary))
    with sqlite3.connect(root / "evidence.sqlite3") as connection:
        connection.execute(
            "UPDATE runs SET source_manifest_hash = ?",
            (hashlib.sha256(canonical.encode()).hexdigest(),),
        )
        connection.execute(
            "UPDATE source_files SET sha256 = ? WHERE source_type = ?",
            (
                digest(phase_path),
                "model_phase_prediction",
            ),
        )


@pytest.fixture
def native_model_service(strict_service):
    service, root, ray_dir = strict_service
    summary = json.loads((ray_dir / "ray_v3_summary.json").read_text())
    authority = summary["geometry_provenance"]
    authority.update(
        status="verified_anatomy_model_sources",
        real_R4_validated=False,
        model_parameter_sources_verified=True,
        recording_geometry_verified=False,
        geometry_quality="unverified_new_recording",
    )
    clock = summary["clock_alignment"]
    inventory = [{"file_name": "generated.h5", "sha256": "a" * 64, "size_bytes": 1}]
    inventory_digest = hashlib.sha256(
        json.dumps(
            inventory,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    manifest = {
        "schema": "generated_hdf5_clock_v1",
        "source": "generated_hdf5_native_alignment",
        "raw_run_dir": str(root / "generated_raw"),
        "alignment_file": "alignment.csv",
        "alignment_sha256": clock["sha256"],
        "frame_count": clock["frame_count"],
        "source_inventory": inventory,
        "source_inventory_sha256": inventory_digest,
        "training_reference_clock": False,
    }
    manifest_path = root / "hdf5_clock_manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    clock.update(
        source="generated_hdf5_native_alignment",
        generated_manifest_path=str(manifest_path),
        generated_manifest_sha256=digest(manifest_path),
        source_inventory_sha256=inventory_digest,
        source_raw_run_dir=manifest["raw_run_dir"],
        training_reference_clock=False,
        native_correspondence_verified=True,
    )
    rebind_generated_fixture(root, ray_dir, authority, clock)
    return service, root, ray_dir, authority, clock


def test_new_model_source_geometry_and_native_clock_are_strictly_bound(native_model_service):
    service, _, _, _, _ = native_model_service
    result = service.bootstrap()
    assert result["under_ledge_screening"]["binding_validation"]["status"] == "verified"
    assert len(result["coaching_plan"]["items"]) == 1


@pytest.mark.parametrize(
    "attack",
    [
        "csv_changed",
        "manifest_changed",
        "manifest_relocated",
        "inventory_changed",
        "wrong_raw_directory",
        "recording_verified_spoof",
        "native_flag_missing",
        "training_reference_spoof",
        "manifest_csv_hash_disagrees",
    ],
)
def test_new_model_native_clock_cannot_bypass_strict_binding(native_model_service, attack):
    service, root, ray_dir, authority, clock = native_model_service
    assert (
        service.bootstrap()["under_ledge_screening"]["binding_validation"]["status"] == "verified"
    )
    manifest_path = Path(clock["generated_manifest_path"])
    if attack == "csv_changed":
        Path(clock["path"]).write_text("changed\n")
    elif attack == "manifest_changed":
        manifest_path.write_text("{}")
    elif attack == "manifest_relocated":
        other = root / "different_manifest.json"
        shutil.copyfile(manifest_path, other)
        clock["generated_manifest_path"] = str(other)
    elif attack in {"inventory_changed", "manifest_csv_hash_disagrees"}:
        manifest = json.loads(manifest_path.read_text())
        if attack == "inventory_changed":
            manifest["source_inventory"][0]["sha256"] = "b" * 64
        else:
            manifest["alignment_sha256"] = "c" * 64
        manifest_path.write_text(json.dumps(manifest))
        clock["generated_manifest_sha256"] = digest(manifest_path)
    elif attack == "wrong_raw_directory":
        clock["source_raw_run_dir"] = str(root / "other_raw")
    elif attack == "recording_verified_spoof":
        authority["recording_geometry_verified"] = True
    elif attack == "native_flag_missing":
        del clock["native_correspondence_verified"]
    else:
        clock["training_reference_clock"] = True
    rebind_generated_fixture(root, ray_dir, authority, clock)
    result = service.bootstrap()
    assert result["detection_status"]["under_ledge"]["reason"] == "strict_ray_binding_mismatch"
    assert not result["coaching_plan"]["items"]
