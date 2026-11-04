"""Independent regression checks for source binding and saved-policy propagation."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_r4_provenance as source_fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_system.web import FeedbackService  # noqa: E402
from evidence_system.frame_pipeline.phase_geometry import SYNTHETIC_PROVENANCE  # noqa: E402
from evidence_system.r4_provenance import validate_production_geometry  # noqa: E402


def test_bootstrap_preserves_saved_coaching_policy_without_rewriting_it(tmp_path):
    db = tmp_path / "evidence.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript((ROOT / "src/evidence_system/storage/schema.sql").read_text())
        connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            "synthetic:case", "synthetic", "case", "generated", "trial", 0, "", "test",
        ))
    service = FeedbackService(
        mode="recommendations", db_path=db, run_id="case", video_path=None,
        video_manifest_path=None, ray_v3_dir=None, cohort_benchmark_path=None,
        available_runs=[], display_name="Generated recording", interface_version="test",
    )
    folder = tmp_path / "stages/08_under_ledge_ray"
    folder.mkdir(parents=True)
    saved_policy = {
        "version": "ray_v3_coaching_duration_0p3_user_20261103_v1",
        "change_source": "explicit_user_instruction", "change_date": "2026-11-03",
        "changed_threshold": "coaching_minimum_episode_duration_s",
        "previous_value_s": 0.75, "current_value_s": 0.3,
        "other_threshold_values_unchanged": True,
    }
    # Exercise the real-data receipt schema without claiming that these generated
    # test identifiers/hashes constitute a validated historical calibration.
    saved_provenance = {
        "schema": "production_geometry_provenance_v1", "status": "verified_R4_v3_sources",
        "global_run_id": "synthetic:case", "real_R4_validated": True,
        "actual_specimen": "generated", "geometry_quality": "needs_review",
        "source_files": {"correction_table": {"sha256": "a" * 64}},
        "grid_resolution_xyz": [4, 6, 8],
        "scientific_all_recording_geometry": False,
        "user_review_performed": False, "clinician_review_performed": False,
    }
    summary_path = folder / "ray_v3_summary.json"
    summary_path.write_text(json.dumps({
        "run_id": "case", "coaching_policy": saved_policy,
        "geometry_provenance": saved_provenance,
        "config": {"coaching_minimum_episode_duration_s": 0.3},
        "coaching_eligible": True, "evaluable_frame_count": 5,
    }))
    (folder / "ray_v3_candidate_episodes.json").write_text("[]")
    original = summary_path.read_bytes()
    payload = service.bootstrap()
    assert payload["under_ledge_screening"]["provenance"]["coaching_policy"] == saved_policy
    assert payload["under_ledge_screening"]["provenance"]["geometry_provenance"] == saved_provenance
    assert payload["under_ledge_screening"]["config"]["coaching_minimum_episode_duration_s"] == 0.3
    assert summary_path.read_bytes() == original


@pytest.mark.parametrize("attack", ["production_dataset", "historical_run_id", "registry_raw_folder", "source_raw_folder"])
def test_real_identity_cannot_become_synthetic_by_changing_provenance_flags(tmp_path, monkeypatch, attack):
    record = SimpleNamespace(
        dataset_id="synthetic", run_id="generated_case", global_run_id="synthetic:generated_case",
        anatomy_key="generated", edt_profile="generated_grid",
        anatomy_mapping_status="synthetic", source_status="generated",
        raw_run_relpath="generated_case",
    )
    sources = SimpleNamespace(raw_run_dir=tmp_path / "generated_case")
    generated_id = f"P{98}T{97}"
    historical_folder = "Generated/2099-01-02 03_04_05_anatX_haptic_" + generated_id
    if attack == "production_dataset":
        record.dataset_id = "R4"
        record.global_run_id = "R4:generated_case"
    elif attack == "historical_run_id":
        record.run_id = generated_id
        record.global_run_id = "synthetic:" + generated_id
    elif attack == "registry_raw_folder":
        record.raw_run_relpath = historical_folder
    else:
        sources.raw_run_dir = tmp_path / historical_folder
    monkeypatch.setattr("evidence_system.r4_provenance.load_anatomy_profile", lambda *_: {
        "provenance": SYNTHETIC_PROVENANCE,
    })
    with pytest.raises(ValueError, match="synthetic|Synthetic|real|R4|fixture"):
        validate_production_geometry(record, sources, project_root=tmp_path)


@pytest.fixture
def authority_case(tmp_path, monkeypatch):
    # Reuse generated source assets, then independently mutate bindings not
    # covered by the source module's own tests. No real dataset is read here.
    monkeypatch.setattr(source_fixtures.tempfile, "tempdir", str(tmp_path))
    case = source_fixtures.ProvenanceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


@pytest.mark.parametrize("field,value,message", [
    ("actual_specimen", "OTHER_GENERATED_SPECIMEN", "ActualSpecimen"),
    ("calibration_recording_id", f"P{998}T{998}", "different recording"),
    ("geometry_quality", "accepted", "ReviewStatus"),
])
def test_self_consistent_configuration_cannot_replace_authority_identity(authority_case, field, value, message):
    authority_case.profile["phase_signal_geometry"][field] = value
    with pytest.raises(ValueError, match=message):
        authority_case.validate()


def test_changed_grid_header_is_rejected_even_if_declared_file_hash_is_updated(authority_case):
    path = authority_case.edt / "Bone.edt"
    header = b"G3\n1 FLOAT\n4 6 8\n" + b"0 " * 15 + b"1\n"
    path.write_bytes(header + b"\0" * (4 * 6 * 8 * 4))
    authority_case.profile["phase_signal_geometry"]["source_edt_sha256"]["Bone.edt"] = source_fixtures.digest(path)
    with pytest.raises(ValueError, match="resolution disagree"):
        authority_case.validate()


def test_non_cubic_grids_require_and_preserve_their_actual_resolution(authority_case):
    dimensions = [0.256, 0.384, 0.512]
    contract = authority_case.profile["phase_signal_geometry"]
    authority_case.profile["volume_dimensions_sim"] = dimensions
    contract["volume_dimensions_sim"] = dimensions
    contract["grid_resolution_xyz"] = [4, 6, 8]
    authority_case.adf.write_text(authority_case.adf.read_text().replace(
        "{x: 0.256, y: 0.256, z: 0.256}", "{x: 0.256, y: 0.384, z: 0.512}",
    ))
    contract["source_adf_sha256"] = source_fixtures.digest(authority_case.adf)
    header = b"G3\n1 FLOAT\n4 6 8\n" + b"0 " * 15 + b"1\n"
    for path in authority_case.edt.glob("*.edt"):
        path.write_bytes(header + b"\0" * (4 * 6 * 8 * 4))
        contract["source_edt_sha256"][path.name] = source_fixtures.digest(path)
    authority_case.write_catalogue()
    snapshot = authority_case.validate()
    assert snapshot.manifest["grid_resolution_xyz"] == [4, 6, 8]
    assert snapshot.geometry["volume_dimensions_sim"] == dimensions
