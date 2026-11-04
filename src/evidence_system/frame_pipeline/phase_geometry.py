"""Explicit calibrated geometry for the signal-phase model input contract.

Historical R4 features used a run-specific, fixed volume-local-to-world rotation,
a recorder scale independent of the GUI unit, and one EDT voxel spacing in mm.
An ordinary legacy anatomy profile does not establish those semantics. This
module validates an opt-in configuration; it never discovers or writes data.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any

R4_RECORDER_SCALE = 0.049202688
R4_SCHEMA = "r4_signal_geometry_v1"
MODEL_SCHEMA = "r4_anatomy_model_geometry_v1"
SYNTHETIC_SCHEMA = "synthetic_signal_geometry_v1"
SYNTHETIC_PROVENANCE = "deterministically generated synthetic fixture"


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _text(contract: dict[str, Any], name: str) -> str:
    value = contract.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"phase_signal_geometry.{name} is required")
    return value


def _same_number(actual: float, expected: float, name: str) -> None:
    if not math.isclose(actual, expected, abs_tol=1e-12, rel_tol=0):
        raise ValueError(f"Calibrated geometry and anatomy profile disagree: {name}")


def resolve_phase_geometry(
    profile: dict[str, Any], *, global_run_id: str, edt_profile: str
) -> dict[str, Any] | None:
    """Validate an explicitly supplied, run-bound calibrated geometry block.

    Absent blocks preserve the existing manual pipeline. Real automatic inference
    must separately require the returned R4 contract. The synthetic contract is
    accepted only on the repository's explicitly identified generated fixture
    profile. Validation checks configuration consistency; it does not certify a
    new calibration or turn research quality flags into clinical review.
    """
    raw = profile.get("phase_signal_geometry")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("phase_signal_geometry must be an object")
    contract = copy.deepcopy(raw)
    schema = contract.get("schema")
    if schema not in (R4_SCHEMA, MODEL_SCHEMA, SYNTHETIC_SCHEMA):
        raise ValueError("Unsupported signal-phase geometry schema")
    if _text(contract, "global_run_id") != global_run_id:
        raise ValueError("Signal-phase geometry is bound to a different run")
    if _text(contract, "edt_profile") != edt_profile or profile.get("edt_profile") != edt_profile:
        raise ValueError("Signal-phase geometry, anatomy profile and EDT registry disagree")
    _text(contract, "actual_specimen")
    if schema == MODEL_SCHEMA:
        if contract.get("calibration_recording_id") is not None:
            raise ValueError("Model geometry cannot claim another recording's calibration")
        _text(contract, "calibration_scope")
        if (
            contract.get("model_parameter_sources_verified") is not True
            or contract.get("recording_geometry_verified") is not False
        ):
            raise ValueError("Model sources and unverified recording scope must be explicit")
    else:
        _text(contract, "calibration_recording_id")
    _text(contract, "rotation_source")

    recorder_scale = _positive_number(
        contract.get("recorder_scale_m_per_simulator_unit"), "recorder scale"
    )
    spacing = _positive_number(contract.get("edt_distance_scale_mm"), "EDT spacing")
    _same_number(
        recorder_scale,
        _positive_number(profile.get("meters_per_simulator_unit"), "profile recorder scale"),
        "meters_per_simulator_unit",
    )
    _same_number(
        spacing,
        _positive_number(profile.get("edt_distance_scale_mm"), "profile EDT spacing"),
        "edt_distance_scale_mm",
    )
    dimensions = contract.get("volume_dimensions_sim")
    profile_dimensions = profile.get("volume_dimensions_sim")
    if (
        not isinstance(dimensions, (list, tuple))
        or len(dimensions) != 3
        or not isinstance(profile_dimensions, (list, tuple))
        or len(profile_dimensions) != 3
    ):
        raise ValueError("Calibrated geometry requires three volume dimensions")
    dimensions = [_positive_number(value, "volume dimension") for value in dimensions]
    for actual, expected in zip(dimensions, profile_dimensions, strict=True):
        _same_number(actual, _positive_number(expected, "profile dimension"), "dimensions")

    quaternion = contract.get("volume_quaternion_xyzw")
    if not isinstance(quaternion, (list, tuple)) or len(quaternion) != 4:
        raise ValueError("Calibrated geometry requires a fixed xyzw quaternion")
    try:
        quaternion = [float(value) for value in quaternion]
    except (TypeError, ValueError) as exc:
        raise ValueError("Calibrated quaternion must be finite and unit length") from exc
    norm = math.sqrt(sum(value * value for value in quaternion))
    if not all(math.isfinite(value) for value in quaternion) or not math.isclose(
        norm, 1.0, abs_tol=1e-6, rel_tol=0
    ):
        raise ValueError("Calibrated quaternion must be finite and unit length")
    quaternion = [value / norm for value in quaternion]

    if schema in (R4_SCHEMA, MODEL_SCHEMA):
        _same_number(recorder_scale, R4_RECORDER_SCALE, "R4 common recorder scale")
        if schema == R4_SCHEMA and contract.get("geometry_quality") not in (
            "accepted",
            "needs_review",
        ):
            raise ValueError("R4 geometry quality must preserve accepted or needs_review")
        if (
            schema == MODEL_SCHEMA
            and contract.get("geometry_quality") != "unverified_new_recording"
        ):
            raise ValueError("New model geometry must retain unverified recording quality")
        correction_digest = _text(contract, "correction_table_sha256")
        if re.fullmatch(r"[0-9a-fA-F]{64}", correction_digest) is None:
            raise ValueError("R4 correction table SHA256 is required")
    else:
        if profile.get("provenance") != SYNTHETIC_PROVENANCE:
            raise ValueError("Synthetic geometry is restricted to generated fixture profiles")
        if contract.get("geometry_quality") != "synthetic":
            raise ValueError("Synthetic geometry quality must be explicit")

    contract.update(
        volume_quaternion_xyzw=quaternion,
        volume_dimensions_sim=dimensions,
        recorder_scale_m_per_simulator_unit=recorder_scale,
        edt_distance_scale_mm=spacing,
        orientation_mode=(
            "fixed_model_native_volume_local_to_world_xyzw"
            if schema == MODEL_SCHEMA
            else "fixed_calibrated_volume_local_to_world_xyzw"
        ),
        translation="drill_world_position - recorded_volume_world_position",
        local_coordinate_definition="inverse(fixed_quaternion) * translation / recorder_scale",
        tip_distance_definition="EDT_raw * edt_distance_scale_mm - burr_mm / 2",
        removed_distance_definition="EDT_raw * edt_distance_scale_mm; no burr subtraction",
        removal_volume_definition="frame_removed_voxels * source metadata/voxel_volume",
        removed_voxel_mapping="x=x_sim; y=resolution_y-1-y_sim; z=z_sim",
        contract_validation="Run binding and configuration consistency; no new calibration fit",
        user_review_performed=False,
        clinician_review_performed=False,
    )
    return contract


def validate_v3_volume_contract(frames, profile, *, raw_run_dir):
    """Check v3 source units and fixed volume pose without certifying tool/camera registration.

    A matching model name alone does not establish the recorder scale. Every
    recorded volume pose must match the independently verified current ADF and
    declared scale. Native removal volume is retained as its own measured unit;
    it is not replaced by the cube of EDT spacing.
    """
    from pathlib import Path

    import numpy as np
    import yaml

    from ..hdf5_reader import RecordingReader

    reader = RecordingReader(raw_run_dir)
    if reader.schema != "v3":
        return {"schema": "v3_volume_source_contract_v1", "status": "not_applicable_legacy"}
    geometry = profile.get("phase_signal_geometry")
    if not isinstance(geometry, dict) or geometry.get("schema") not in (R4_SCHEMA, MODEL_SCHEMA):
        raise ValueError("V3 geometry requires a verified real anatomy source contract")
    adf = yaml.safe_load(Path(profile["adf_profile"]).read_text(encoding="utf-8"))
    volume = adf[adf["volumes"][0]]
    scale = _positive_number(geometry["recorder_scale_m_per_simulator_unit"], "recorder scale")
    expected_position = (
        np.asarray([volume["location"]["position"][axis] for axis in "xyz"], dtype=float) * scale
    )
    if not np.isfinite(expected_position).all() or not np.any(np.abs(expected_position) > 1e-8):
        raise ValueError("V3 ADF volume translation cannot establish the recorder scale")
    from ..r4_provenance import _rpy_quaternion

    expected_quaternion = np.asarray(
        _rpy_quaternion(volume["location"]["orientation"]), dtype=float
    )
    source_volumes, readmes = [], []
    for path in reader.paths:
        with reader.open_chunk(path) as handle:
            readme = handle.get("metadata/README")
            voxel = handle.get("metadata/voxel_volume")
            if readme is None or voxel is None:
                raise ValueError(
                    "V3 recording requires explicit README units and voxel-volume metadata"
                )
            text = readme[()]
            text = text.decode("utf-8") if isinstance(text, bytes) else str(text)
            required = (
                "All position information is in meters",
                "[qx, qy, qz, qw]",
                "T_world_obj",
                "Depth in CV convention",
                "T_cv_ambf",
            )
            if any(fragment not in text for fragment in required):
                raise ValueError(
                    "V3 README does not establish the supported metre/xyzw/CV conventions"
                )
            unit = voxel.attrs.get("units", "")
            unit = unit.decode("utf-8") if isinstance(unit, bytes) else str(unit)
            if unit != "mm^3, millimeters cubed":
                raise ValueError(
                    "V3 voxel-volume metadata must explicitly declare cubic millimetres"
                )
            source_volumes.append(_positive_number(voxel[()], "source voxel volume"))
            readmes.append(
                {"source_hdf5": path.name, "units_readme": text, "voxel_volume_units": unit}
            )
    positions = np.asarray(
        [[row[f"volume_{axis}"] for axis in "xyz"] for row in frames], dtype=float
    )
    quaternions = np.asarray(
        [[row[f"volume_q{axis}"] for axis in "xyzw"] for row in frames], dtype=float
    )
    if not len(frames) or not np.isfinite(positions).all() or not np.isfinite(quaternions).all():
        raise ValueError("V3 recorded volume poses must be present and finite")
    norms = np.linalg.norm(quaternions, axis=1)
    if np.any(np.abs(norms - 1) > 1e-6):
        raise ValueError("V3 recorded volume quaternions must be unit length")
    quaternions = quaternions / norms[:, None]
    position_error = float(np.max(np.abs(positions - expected_position)))
    quaternion_error = float(
        np.max(
            np.minimum(
                np.max(np.abs(quaternions - expected_quaternion), axis=1),
                np.max(np.abs(quaternions + expected_quaternion), axis=1),
            )
        )
    )
    if position_error > 1e-8 or quaternion_error > 1e-6:
        raise ValueError(
            "V3 recorded volume pose does not match the current ADF and declared recorder scale"
        )
    if max(source_volumes) - min(source_volumes) > 1e-12:
        raise ValueError("V3 native voxel-volume metadata changes across source chunks")
    spacing = _positive_number(geometry["edt_distance_scale_mm"], "EDT spacing")
    return {
        "schema": "v3_volume_source_contract_v1",
        "status": "volume_source_contract_matched",
        "frame_count": len(frames),
        "chunk_count": len(reader.paths),
        "source_units": readmes,
        "recorder_scale_m_per_simulator_unit": scale,
        "recorder_scale_evidence": (
            "Nonzero recorded volume translation matches current ADF translation "
            "multiplied by the declared scale"
        ),
        "maximum_volume_position_error_m": position_error,
        "maximum_volume_quaternion_component_error": quaternion_error,
        "native_voxel_volume_mm3": source_volumes[0],
        "edt_spacing_mm": spacing,
        "native_volume_divided_by_edt_spacing_cubed": source_volumes[0] / spacing**3,
        "voxel_volume_policy": (
            "Preserve explicit native mm^3; EDT spacing cubed is not substituted"
        ),
        "camera_tool_registration_status": "not_validated_by_volume_contract",
        "burr_size_status": "not_established_by_volume_contract",
        "recording_geometry_verified": False,
        "user_review_performed": False,
        "clinician_review_performed": False,
    }
