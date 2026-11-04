"""Resolve a recording directory and anatomy name to explicit source geometry.

Historical R4 corrections remain recording-specific. New recordings can use a
current model configuration supported by the R4 authority, with an explicit
unverified-recording status. No fitted rotation is transferred between cases.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import r4_provenance as authority
from .frame_pipeline.phase_geometry import MODEL_SCHEMA, R4_SCHEMA, resolve_phase_geometry
from .source_audit import REQUIRED_EDT_STEMS


@dataclass
class AutoAnatomy:
    profile: dict[str, Any]
    metadata: dict[str, Any]
    anatomy_key: str
    edt_dir: Path


def _optional_recording_id(value):
    try:
        return authority._recording_id(value)
    except ValueError:
        return None


def _same_rotation(left, right):
    try:
        authority._quaternion(left, right, "model rotation")
        return True
    except ValueError:
        return False


def _recorded_model(raw, supported_models):
    """Read only the model field; remote ADF paths are provenance, not local files."""
    path = raw / "metadata.json"
    if not path.is_file():
        return None, None
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    value = document.get("volume_adf") if isinstance(document, dict) else None
    if value is None:
        return None, None
    if not isinstance(value, str):
        raise ValueError("metadata.json volume_adf must be an ADF path string")
    basename = value.replace("\\", "/").rsplit("/", 1)[-1]
    match = re.fullmatch(r"volume_256_([A-Za-z0-9_-]+)\.ya?ml", basename, re.I)
    model = match[1].upper() if match else None
    if model not in supported_models:
        raise ValueError(
            f"metadata.json volume_adf does not identify a supported anatomy model: {basename}"
        )
    return model, {
        "path": str(path),
        "sha256": authority._sha(path),
        "field": "volume_adf",
        "value": value,
    }


def configure_anatomy(
    *,
    simulator_root,
    correction_table,
    recording_dir,
    run_id=None,
    dataset_id="LOCAL",
    anatomy_model="auto",
    edt_root=None,
):
    """Read source assets and return a profile; never write or alter source data."""
    import yaml

    root = Path(simulator_root).expanduser().resolve()
    resolved_edt_root = Path(edt_root).expanduser().resolve() if edt_root is not None else root
    table = Path(correction_table).expanduser().resolve()
    raw = Path(recording_dir).expanduser().resolve()
    if not root.is_dir() or not table.is_file() or not raw.is_dir():
        raise ValueError(
            "Simulator root, trusted correction table and recording directory must exist"
        )
    table_hash = authority._sha(table)
    if table_hash not in authority.TRUSTED_R4_CORRECTION_SHA256:
        raise ValueError("Automatic geometry requires the trusted R4 v3 correction table")
    with table.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    identities = [row["RecordingID"] for row in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("The trusted table contains duplicate recording identities")
    folders = [row for row in rows if row["FolderName"].replace(":", "_") == raw.name]
    if len(folders) > 1:
        raise ValueError("Recording directory ambiguously matches the correction table")
    if run_id is None:
        run_id = folders[0]["RecordingID"] if folders else re.sub(r"[^A-Za-z0-9_-]", "_", raw.name)
    run_id, dataset_id = str(run_id).strip(), str(dataset_id).strip()
    if not run_id or not dataset_id:
        raise ValueError("A nonempty recording and dataset identity is required")
    canonical_id = _optional_recording_id(run_id)
    id_rows = [row for row in rows if row["RecordingID"] == canonical_id]
    historical = bool(id_rows or folders)
    model_source = None
    if historical:
        if len(id_rows) != 1 or len(folders) != 1 or id_rows[0] != folders[0]:
            raise ValueError("Known historical recording identity and directory disagree")
        selected_row = id_rows[0]
        model = selected_row["ActualSpecimen"]
        if (
            str(anatomy_model or "auto").casefold() != "auto"
            and str(anatomy_model).upper() != model
        ):
            raise ValueError("Requested anatomy model conflicts with the historical R4 correction")
    else:
        if dataset_id.casefold() in {"r4", "demo", "synthetic", "test_fixture"}:
            raise ValueError("A new real recording needs its own real dataset identity")
        recorded_model, model_source = _recorded_model(raw, {row["ActualSpecimen"] for row in rows})
        if str(anatomy_model or "auto").casefold() == "auto":
            if recorded_model is None:
                raise ValueError(
                    "A new recording requires an explicit supported anatomy model "
                    "when metadata.json has no recognized volume_adf"
                )
            model = recorded_model
        else:
            model = str(anatomy_model).upper()
            if recorded_model is not None and recorded_model != model:
                raise ValueError("Requested anatomy model conflicts with metadata.json volume_adf")
        selected_row = None
    model_rows = [row for row in rows if row["ActualSpecimen"] == model]
    if not model_rows:
        raise ValueError("Anatomy model is not supported by the R4 source catalog")
    representative = selected_row or model_rows[0]
    if not historical:
        for row in model_rows:
            for field in ("RecorderScale", "SpacingMm", "GUIUnit"):
                authority._close(row[field], representative[field], "ambiguous model " + field)
            if row["ActualAnatomy"] != representative["ActualAnatomy"]:
                raise ValueError("Anatomy model has ambiguous GUI anatomy identity")

    gui = root / "scripts" / "study_gui" / "gui_setup.yaml"
    document = yaml.safe_load(gui.read_text(encoding="utf-8"))
    entries = []
    for key in document["volumes"]:
        entry = document[key]
        adf = (gui.parent / entry["adf_path"]).resolve()
        if adf.stem == "volume_256_" + model:
            entries.append((entry, adf))
    if len(entries) != 1:
        raise ValueError("Anatomy model must uniquely match a GUI/ADF entry")
    entry, adf = entries[0]
    edt = authority._gui_edt_directory(entry, resolved_edt_root)
    if not adf.is_relative_to(root):
        raise ValueError("Anatomy ADF must stay inside the explicit simulator root")
    if str(entry["name"]).split()[-1].upper() != representative["ActualAnatomy"].upper():
        raise ValueError("GUI identity disagrees with the trusted anatomy model")
    authority._close(entry["unit"], representative["GUIUnit"], "GUIUnit")
    text = adf.read_text(encoding="utf-8")
    adf_document = yaml.safe_load(text)
    volume = adf_document[adf_document["volumes"][0]]
    dimensions = [float(volume["dimensions"][axis]) for axis in "xyz"]
    hashes, resolutions = {}, []
    for stem in sorted(REQUIRED_EDT_STEMS):
        path = edt / (stem + ".edt")
        if not path.is_file():
            raise ValueError(f"Missing required anatomy EDT: {path.name}")
        resolutions.append(authority._header(path))
        hashes[path.name] = authority._sha(path)
    if len(set(resolutions)) != 1:
        raise ValueError("Anatomy EDT grid resolutions disagree")
    resolution = resolutions[0]
    catalogue, catalogue_hash = authority._trusted_edt_profile(model, edt.name, resolution, hashes)
    spacing = dimensions[0] / (resolution[0] * float(entry["unit"]))
    authority._close(spacing, representative["SpacingMm"], "model dimension/grid/GUI spacing")
    if historical:
        quaternion = json.loads(selected_row["UsedRotation"])
        authority._quaternion(
            quaternion,
            authority._rotation_from_source(selected_row, adf, text, volume),
            "rotation source",
        )
        rotation_source = selected_row["RotationSource"]
        references = [selected_row["RecordingID"]]
    else:
        quaternion = authority._rpy_quaternion(volume["location"]["orientation"])
        rotation_source = f"ADF/{adf.name}:current"
        references = [
            row["RecordingID"]
            for row in model_rows
            if row["RotationSource"] == rotation_source
            and _same_rotation(json.loads(row["UsedRotation"]), quaternion)
        ]
        if not references:
            raise ValueError(
                "Current model-native ADF rotation has no supporting trusted R4 record; "
                "a recording-specific calibration is required"
            )
    files = {
        "correction_table": {"path": str(table), "sha256": table_hash},
        "source_gui": {"path": str(gui), "sha256": authority._sha(gui)},
        "source_adf": {"path": str(adf), "sha256": authority._sha(adf)},
        "edt_source_catalogue": {"path": str(catalogue), "sha256": catalogue_hash},
        **{name: {"path": str(edt / name), "sha256": value} for name, value in hashes.items()},
    }
    if model_source:
        files["recording_metadata"] = {key: model_source[key] for key in ("path", "sha256")}
    contract = {
        "schema": R4_SCHEMA if historical else MODEL_SCHEMA,
        "global_run_id": f"{dataset_id}:{run_id}",
        "edt_profile": edt.name,
        "actual_specimen": model,
        "calibration_recording_id": canonical_id if historical else None,
        "rotation_source": rotation_source,
        "volume_quaternion_xyzw": quaternion,
        "recorder_scale_m_per_simulator_unit": float(representative["RecorderScale"]),
        "edt_distance_scale_mm": float(representative["SpacingMm"]),
        "volume_dimensions_sim": dimensions,
        "grid_resolution_xyz": list(resolution),
        "geometry_quality": selected_row["ReviewStatus"]
        if historical
        else "unverified_new_recording",
        "source_correction_table": str(table),
        "correction_table_sha256": table_hash,
        "source_simulator_root": str(root),
        "source_edt_root": str(resolved_edt_root),
        "source_gui_path": str(gui),
        "source_gui_sha256": files["source_gui"]["sha256"],
        "source_adf_path": str(adf),
        "source_adf_sha256": files["source_adf"]["sha256"],
        "source_edt_sha256": hashes,
    }
    if not historical:
        contract.update(
            model_parameter_sources_verified=True,
            recording_geometry_verified=False,
            model_parameter_reference_recording_ids=sorted(references),
            recording_configuration_assumption=(
                "Recording uses this current simulator GUI/ADF configuration"
            ),
            calibration_scope="model_native_parameters_not_individual_recording_calibration",
        )
    profile = {
        "edt_profile": edt.name,
        "spatial_resolution_mm": contract["edt_distance_scale_mm"],
        "edt_distance_scale_mm": contract["edt_distance_scale_mm"],
        "simulator_unit": float(entry["unit"]),
        "meters_per_simulator_unit": contract["recorder_scale_m_per_simulator_unit"],
        "volume_dimensions_sim": dimensions,
        "adf_profile": str(adf),
        "voxel_coordinate_mapping": "x_edt=x_sim; y_edt=(resolution_y-1)-y_sim; z_edt=z_sim",
        "provenance": (
            "Automatically resolved trusted anatomy sources; recording verification "
            "scope is explicit"
        ),
        "phase_signal_geometry": contract,
    }
    anatomy_key = "auto_" + re.sub(r"[^A-Za-z0-9_-]", "_", f"{dataset_id}_{run_id}")
    metadata = {
        "schema": "automatic_anatomy_configuration_v1",
        "mode": "historical_r4_recording" if historical else "new_recording_model_geometry",
        "run_id": run_id,
        "dataset_id": dataset_id,
        "global_run_id": contract["global_run_id"],
        "anatomy_key": anatomy_key,
        "actual_specimen": model,
        "recording_dir": str(raw),
        "source_edt_root": str(resolved_edt_root),
        "source_files": files,
        "grid_resolution_xyz": list(resolution),
        "model_reference_recording_ids": sorted(references),
        "historical_rotation_sources": dict(Counter(row["RotationSource"] for row in model_rows)),
        "correction_quality_counts": dict(Counter(row["ReviewStatus"] for row in rows)),
        "user_review_performed": False,
        "clinician_review_performed": False,
    }
    if not historical:
        metadata["model_selection_source"] = model_source or {
            "field": "anatomy_model",
            "value": model,
        }
    return AutoAnatomy(profile, metadata, anatomy_key, edt)


def validate_model_geometry(record, sources, profile, geometry, *, project_root):
    """Re-derive generated model parameters; caller-written claims are insufficient."""
    expected = configure_anatomy(
        simulator_root=geometry["source_simulator_root"],
        edt_root=geometry.get("source_edt_root"),
        correction_table=geometry["source_correction_table"],
        recording_dir=sources.raw_run_dir,
        run_id=record.run_id,
        dataset_id=record.dataset_id,
        anatomy_model=geometry["actual_specimen"],
    )
    if expected.metadata["mode"] != "new_recording_model_geometry":
        raise ValueError("A historical recording cannot use the new model-geometry mode")
    # Saved v1 profiles predate independent EDT roots. They retain the exact
    # simulator-root-relative source contract and their original receipt shape.
    if "source_edt_root" not in geometry:
        expected.profile["phase_signal_geometry"].pop("source_edt_root")
    if (
        sources.edt_dir.resolve() != expected.edt_dir
        or Path(record.raw_run_relpath).name != sources.raw_run_dir.name
    ):
        raise ValueError("Model geometry source directory or registry binding differs")
    expected_geometry = resolve_phase_geometry(
        expected.profile, global_run_id=record.global_run_id, edt_profile=record.edt_profile
    )
    if geometry != expected_geometry:
        raise ValueError("Model geometry differs from automatically verified source parameters")
    for key, value in expected.profile.items():
        if profile.get(key) != value:
            raise ValueError(f"Model anatomy profile differs from verified sources: {key}")
    files = dict(expected.metadata["source_files"])
    if profile.get("config_path"):
        path = Path(profile["config_path"])
        files["profile_configuration"] = {"path": str(path), "sha256": authority._sha(path)}
    stats = {
        item["path"]: (Path(item["path"]).stat().st_size, Path(item["path"]).stat().st_mtime_ns)
        for item in files.values()
    }
    manifest = {
        "schema": "production_geometry_provenance_v1",
        "status": "verified_anatomy_model_sources",
        "global_run_id": record.global_run_id,
        "recording_id": record.run_id,
        "actual_specimen": geometry["actual_specimen"],
        "geometry_quality": "unverified_new_recording",
        "real_R4_validated": False,
        "model_parameter_sources_verified": True,
        "recording_geometry_verified": False,
        "source_files": files,
        "grid_resolution_xyz": geometry["grid_resolution_xyz"],
        "geometry_contract": geometry,
        "model_reference_recording_ids": expected.metadata["model_reference_recording_ids"],
        "correction_quality_counts": expected.metadata["correction_quality_counts"],
        "scientific_all_recording_geometry": False,
        "user_review_performed": False,
        "clinician_review_performed": False,
        "scope": (
            "Current model configuration has trusted R4 support; this new recording "
            "has no individual R4 calibration"
        ),
        "recording_configuration_assumption": geometry["recording_configuration_assumption"],
        "native_quantity_policy": (
            "Preserve recorded world-metre motion, camera calibration, depth units "
            "and metadata/voxel_volume; these are not EDT millimetres"
        ),
    }
    return authority.GeometrySnapshot(profile, geometry, manifest, stats)
