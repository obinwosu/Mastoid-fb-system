"""Automatic anatomy resolution preserves historical and new-recording scopes."""

from __future__ import annotations

import csv
import json
import os
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_r4_provenance as fixtures

from evidence_system import r4_provenance as authority
from evidence_system.anatomy_catalog import configure_anatomy
from evidence_system.frame_pipeline.phase_geometry import MODEL_SCHEMA
from evidence_system.r4_provenance import validate_production_geometry


@pytest.fixture
def source_catalog():
    base = fixtures.ProvenanceTests()
    base.setUp()
    base.gui = base.adf.parent / "scripts/study_gui/gui_setup.yaml"
    base.gui.parent.mkdir(parents=True)
    base.gui.write_text(
        "volumes: [v]\nv:\n  adf_path: ../../volume_256_TESTSPECIMEN.yaml\n"
        "  sdf_path: /edt_fixture/\n  unit: 0.064\n  name: Anatomy Z\n"
    )
    try:
        yield base
    finally:
        base.doCleanups()


def configure(base, *, raw=None, run_id=None, model="auto", dataset="HDF5", edt_root=None):
    with (
        patch(
            "evidence_system.r4_provenance.TRUSTED_R4_CORRECTION_SHA256",
            {fixtures.digest(base.table)},
        ),
        patch("evidence_system.r4_provenance.EDT_CATALOG_PATH", base.catalogue),
        patch(
            "evidence_system.r4_provenance.TRUSTED_EDT_CATALOG_SHA256",
            fixtures.digest(base.catalogue),
        ),
    ):
        return configure_anatomy(
            simulator_root=base.adf.parent,
            correction_table=base.table,
            recording_dir=raw or base.raw,
            run_id=run_id,
            dataset_id=dataset,
            anatomy_model=model,
            edt_root=edt_root,
        )


def new_directory(base):
    raw = base.root / "new_session"
    raw.mkdir()
    return raw


def rewrite_rows(base, transform):
    with base.table.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    transform(rows)
    with base.table.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    base.write_catalogue()


def validate_generated(base, result, raw, edit=None):
    if edit:
        edit(result.profile)
    base.config.write_text(json.dumps({"profiles": {result.anatomy_key: result.profile}}))
    record = SimpleNamespace(
        dataset_id="HDF5",
        run_id=result.metadata["run_id"],
        global_run_id=result.metadata["global_run_id"],
        anatomy_key=result.anatomy_key,
        edt_profile=result.edt_dir.name,
        raw_run_relpath=raw.name,
        anatomy_mapping_status="configured_automatic_sources",
        source_status="user_selected",
    )
    with (
        patch.dict(os.environ, {"MASTOID_ANATOMY_PROFILES": str(base.config)}),
        patch(
            "evidence_system.r4_provenance.TRUSTED_R4_CORRECTION_SHA256",
            {fixtures.digest(base.table)},
        ),
        patch("evidence_system.r4_provenance.EDT_CATALOG_PATH", base.catalogue),
        patch(
            "evidence_system.r4_provenance.TRUSTED_EDT_CATALOG_SHA256",
            fixtures.digest(base.catalogue),
        ),
    ):
        return validate_production_geometry(
            record,
            SimpleNamespace(raw_run_dir=raw, edt_dir=result.edt_dir),
            project_root=base.root,
        )


def test_historical_auto_keeps_record_binding_in_hdf5_dataset(source_catalog):
    base = source_catalog
    resolved = configure(base)
    assert resolved.metadata["mode"] == "historical_r4_recording"
    assert (
        resolved.profile["phase_signal_geometry"]["calibration_recording_id"] == base.record.run_id
    )
    snapshot = validate_generated(base, resolved, base.raw)
    assert snapshot.manifest["status"] == "verified_R4_v3_sources"
    assert snapshot.manifest["geometry_quality"] == "needs_review"
    assert snapshot.manifest["scientific_all_recording_geometry"] is False


@pytest.mark.parametrize("attack", ["wrong_model", "renamed_id", "renamed_folder"])
def test_historical_identity_cannot_escape_its_specific_correction(source_catalog, attack):
    base = source_catalog
    kwargs = {"run_id": base.record.run_id}
    if attack == "wrong_model":
        kwargs["model"] = "OTHER"
    elif attack == "renamed_id":
        kwargs["run_id"] = "new_case"
    else:
        kwargs["raw"] = new_directory(base)
    with pytest.raises(ValueError, match="conflicts|disagree"):
        configure(base, **kwargs)


def test_new_model_uses_current_sources_and_never_claims_recording_validation(source_catalog):
    base = source_catalog
    raw = new_directory(base)
    result = configure(base, raw=raw, model="testspecimen")
    geometry = result.profile["phase_signal_geometry"]
    assert geometry["schema"] == MODEL_SCHEMA
    assert geometry["calibration_recording_id"] is None
    assert geometry["recording_geometry_verified"] is False
    snapshot = validate_generated(base, result, raw)
    assert snapshot.manifest["status"] == "verified_anatomy_model_sources"
    assert snapshot.manifest["real_R4_validated"] is False
    assert snapshot.manifest["model_parameter_sources_verified"] is True
    assert snapshot.manifest["recording_geometry_verified"] is False
    assert snapshot.manifest["geometry_quality"] == "unverified_new_recording"
    assert not snapshot.manifest["clinician_review_performed"]


@pytest.mark.parametrize(
    "adf",
    [
        "/sim/ADF/volume_256_TESTSPECIMEN.yaml",
        "\\".join(["D:", "sim", "ADF", "volume_256_TESTSPECIMEN.yaml"]),
    ],
)
def test_new_recording_selects_metadata_model_and_preserves_source_hash(source_catalog, adf):
    base = source_catalog
    raw = new_directory(base)
    metadata = raw / "metadata.json"
    metadata.write_text(json.dumps({"volume_adf": adf, "notes": "not a model input"}))
    result = configure(base, raw=raw)
    assert result.metadata["actual_specimen"] == "TESTSPECIMEN"
    assert result.metadata["model_selection_source"]["value"] == adf
    assert "notes" not in json.dumps(result.metadata)
    snapshot = validate_generated(base, result, raw)
    assert snapshot.manifest["source_files"]["recording_metadata"]["sha256"] == fixtures.digest(
        metadata
    )
    assert snapshot.manifest["recording_geometry_verified"] is False


def test_explicit_model_cannot_override_conflicting_recording_metadata(source_catalog):
    base = source_catalog
    raw = new_directory(base)
    (raw / "metadata.json").write_text(
        json.dumps({"volume_adf": "/sim/volume_256_TESTSPECIMEN.yaml"})
    )
    with pytest.raises(ValueError, match="conflicts with metadata"):
        configure(base, raw=raw, model="OTHER")


def test_unknown_recorded_model_is_not_silently_replaced(source_catalog):
    base = source_catalog
    raw = new_directory(base)
    (raw / "metadata.json").write_text(json.dumps({"volume_adf": "/sim/volume_256_OTHER.yaml"}))
    with pytest.raises(ValueError, match="does not identify a supported"):
        configure(base, raw=raw, model="TESTSPECIMEN")


@pytest.mark.parametrize("model,dataset", [("auto", "HDF5"), ("TESTSPECIMEN", "R4")])
def test_new_recording_requires_explicit_model_and_distinct_scope(source_catalog, model, dataset):
    with pytest.raises(ValueError, match="requires an explicit|own real dataset"):
        configure(source_catalog, raw=new_directory(source_catalog), model=model, dataset=dataset)


def test_new_model_never_borrows_a_historical_fitted_rotation(source_catalog):
    base = source_catalog
    rewrite_rows(base, lambda rows: rows[0].update(RotationSource="fitted"))
    with pytest.raises(ValueError, match="no supporting trusted R4"):
        configure(base, raw=new_directory(base), model="TESTSPECIMEN")


def test_new_model_accepts_history_from_a_commented_adf_line_with_the_same_rotation(source_catalog):
    # The R4 RT143 rows cite ADF line 14 (commented); today's active line holds the same angles.
    base = source_catalog
    rewrite_rows(base, lambda rows: rows[0].update(
        RotationSource="ADF/volume_256_TESTSPECIMEN.yaml:commented:L14"))
    configure(base, raw=new_directory(base), model="TESTSPECIMEN")


def test_current_model_can_coexist_with_fitted_history_without_selecting_it(source_catalog):
    base = source_catalog

    def augment(rows):
        other = dict(
            rows[0],
            RecordingID="P" + "8" * 3 + "T" + "8" * 3,
            FolderName="another_historical",
            RotationSource="fitted",
            UsedRotation="[0,0,1,0]",
            FittedRotation="[0,0,1,0]",
        )
        rows.append(other)

    rewrite_rows(base, augment)
    result = configure(base, raw=new_directory(base), model="TESTSPECIMEN")
    assert result.profile["phase_signal_geometry"]["volume_quaternion_xyzw"] == [0, 0, 0, 1]
    assert result.metadata["model_reference_recording_ids"] == [base.record.run_id]


def test_conflicting_model_scale_rejects_automatic_transfer(source_catalog):
    base = source_catalog

    def augment(rows):
        rows.append(dict(rows[0], RecordingID="OTHER", FolderName="other", SpacingMm="2.0"))

    rewrite_rows(base, augment)
    with pytest.raises(ValueError, match="ambiguous model SpacingMm"):
        configure(base, raw=new_directory(base), model="TESTSPECIMEN")


@pytest.mark.parametrize("attack", ["quaternion", "spacing", "edt_hash", "review_claim"])
def test_generated_profile_is_rederived_instead_of_trusting_caller_claims(source_catalog, attack):
    base = source_catalog
    raw = new_directory(base)
    result = configure(base, raw=raw, model="TESTSPECIMEN")

    def change(profile):
        geometry = profile["phase_signal_geometry"]
        if attack == "quaternion":
            geometry["volume_quaternion_xyzw"] = [0, 0, 1, 0]
        elif attack == "spacing":
            geometry["edt_distance_scale_mm"] = profile["edt_distance_scale_mm"] = 2
        elif attack == "edt_hash":
            geometry["source_edt_sha256"]["Bone.edt"] = "0" * 64
        else:
            geometry["recording_geometry_verified"] = True

    with pytest.raises(ValueError, match="differs|unverified recording"):
        validate_generated(base, result, raw, change)


def test_non_cubic_source_grids_and_missing_edt_are_handled_explicitly(source_catalog):
    base = source_catalog
    header = b"G3\n1 FLOAT\n4 4 3\n" + b"0 " * 15 + b"1\n"
    for path in base.edt.glob("*.edt"):
        path.write_bytes(header + b"\0" * (4 * 4 * 3 * 4))
    contract = base.profile["phase_signal_geometry"]
    contract["grid_resolution_xyz"] = [4, 4, 3]
    contract["source_edt_sha256"] = {p.name: fixtures.digest(p) for p in base.edt.glob("*.edt")}
    base.write_catalogue()
    result = configure(base, raw=new_directory(base), model="TESTSPECIMEN")
    assert result.metadata["grid_resolution_xyz"] == [4, 4, 3]
    (base.edt / "Bone.edt").unlink()
    with pytest.raises(ValueError, match="Missing required anatomy EDT"):
        configure(base)


@pytest.mark.parametrize("new_recording", [False, True])
def test_independent_edt_root_preserves_model_content_and_recording_scope(
    source_catalog, new_recording
):
    base = source_catalog
    raw = new_directory(base) if new_recording else base.raw
    separate = base.root / "relocated_edt_root"
    shutil.copytree(base.edt, separate / base.edt.name)
    result = configure(base, raw=raw, model="TESTSPECIMEN", edt_root=separate)
    assert result.edt_dir == separate / base.edt.name
    assert result.profile["phase_signal_geometry"]["source_edt_root"] == str(separate)
    snapshot = validate_generated(base, result, raw)
    assert snapshot.manifest["real_R4_validated"] is not new_recording
    assert snapshot.manifest["source_files"]["Bone.edt"]["path"] == str(
        separate / base.edt.name / "Bone.edt"
    )
    assert "edt_source_catalogue" in snapshot.manifest["source_files"]


@pytest.mark.parametrize("new_recording", [False, True])
def test_saved_profiles_without_edt_root_keep_original_source_contract(
    source_catalog, new_recording
):
    base = source_catalog
    raw = new_directory(base) if new_recording else base.raw
    result = configure(base, raw=raw, model="TESTSPECIMEN")
    del result.profile["phase_signal_geometry"]["source_edt_root"]
    snapshot = validate_generated(base, result, raw)
    assert "source_edt_root" not in snapshot.geometry
    assert snapshot.manifest["source_files"]["Bone.edt"]["path"] == str(base.edt / "Bone.edt")


@pytest.mark.parametrize("attack", ["wrong_profile_name", "same_name_wrong_content"])
def test_separate_root_does_not_accept_a_different_or_renamed_model(source_catalog, attack):
    base = source_catalog
    separate = base.root / "relocated_edt_root"
    selected = separate / ("another_model" if attack == "wrong_profile_name" else base.edt.name)
    shutil.copytree(base.edt, selected)
    if attack == "same_name_wrong_content":
        bone = selected / "Bone.edt"
        contents = bone.read_bytes()
        bone.write_bytes(contents[:-4] + b"\0\0\x80?")
    with pytest.raises(ValueError, match="Missing required anatomy EDT|trusted anatomy model"):
        configure(base, edt_root=separate)


def test_versioned_catalogue_accepts_checkout_line_endings_but_rejects_content_changes(tmp_path):
    original = authority.EDT_CATALOG_PATH.read_bytes().replace(b"\r\n", b"\n")
    row = json.loads(original)["models"]["LT151"]
    copy = tmp_path / "catalogue.json"
    with patch("evidence_system.r4_provenance.EDT_CATALOG_PATH", copy):
        for ending in (b"\n", b"\r\n"):
            copy.write_bytes(original.replace(b"\n", ending))
            authority._trusted_edt_profile(
                "LT151", row["edt_profile"], row["grid_resolution_xyz"], row["sha256"]
            )
        copy.write_bytes(original.replace(b"LT151", b"OTHER", 1))
        with pytest.raises(ValueError, match="trusted version"):
            authority._trusted_edt_profile(
                "LT151", row["edt_profile"], row["grid_resolution_xyz"], row["sha256"]
            )
