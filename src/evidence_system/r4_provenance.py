"""Fail-closed production binding to the published R4 geometry authority.

Low-level historical analysis helpers remain callable separately. Production
builds require this verification, or an explicitly identified synthetic fixture.
No path is discovered from a user's home directory or an adjacent checkout.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .frame_pipeline.edt_enrichment import load_anatomy_profile
from .frame_pipeline.phase_geometry import (
    MODEL_SCHEMA,
    R4_SCHEMA,
    SYNTHETIC_PROVENANCE,
    SYNTHETIC_SCHEMA,
    resolve_phase_geometry,
)
from .project import RULES_DIR
from .source_audit import REQUIRED_EDT_STEMS

# geometry_corrections_v3: R4 numeric corrections plus retained review/initial-
# burr evidence. A caller cannot replace the authority with an arbitrary table.
TRUSTED_R4_CORRECTION_SHA256 = frozenset(
    {"eb635245a9c1b2f7dc4df007a59ba3f8958a75ba08a0826765444947e8c2e080"}
)
EDT_CATALOG_PATH = RULES_DIR / "r4_edt_sources.json"
# Normalize only repository line endings for cross-platform checkout portability.
TRUSTED_EDT_CATALOG_SHA256 = "8425646014984b1d0dd909ddd675cb1307d15239f32a7ad413c953e5ce07c9e0"


@dataclass
class GeometrySnapshot:
    profile: dict[str, Any]
    geometry: dict[str, Any] | None
    manifest: dict[str, Any]
    source_stats: dict[str, tuple[int, int]]


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _close(actual, expected, name, tolerance=1e-10):
    try:
        valid = math.isfinite(float(actual)) and math.isclose(
            float(actual), float(expected), rel_tol=0, abs_tol=tolerance
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"R4 authority mismatch: {name}")


def _quaternion(actual, expected, name):
    if len(actual) != 4 or len(expected) != 4:
        raise ValueError(f"Invalid quaternion: {name}")
    error = min(
        max(abs(float(a) - sign * float(b)) for a, b in zip(actual, expected, strict=True))
        for sign in (-1, 1)
    )
    if not math.isfinite(error) or error > 1e-10:
        raise ValueError(f"R4 authority mismatch: {name}")


def _recording_id(value):
    match = re.fullmatch(r"P0*(\d+)_?T0*(\d+)", str(value), flags=re.I)
    if not match:
        raise ValueError("Real R4 geometry requires an unambiguous historical recording ID")
    return f"P{int(match[1])}T{int(match[2])}"


def _path(value, project_root, name, *, directory=False):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"R4 provenance requires {name}")
    result = Path(value).expanduser()
    result = (result if result.is_absolute() else Path(project_root) / result).resolve()
    if not (result.is_dir() if directory else result.is_file()):
        raise ValueError(f"R4 source is unavailable: {name}")
    return result


def _header(path):
    with path.open("rb") as stream:

        def token():
            byte = stream.read(1)
            while byte and byte.isspace():
                byte = stream.read(1)
            value = bytearray()
            while byte and not byte.isspace():
                value.extend(byte)
                byte = stream.read(1)
            if not value:
                raise ValueError("Incomplete EDT header")
            return value.decode("ascii")

        if [token(), token(), token()] != ["G3", "1", "FLOAT"]:
            raise ValueError("R4 EDT requires a G3 scalar FLOAT grid")
        resolution = tuple(int(token()) for _ in range(3))
        if any(size <= 0 for size in resolution):
            raise ValueError("Invalid EDT resolution")
        for _ in range(16):
            float(token())
        payload = stream.tell()
    if path.stat().st_size != payload + math.prod(resolution) * 4:
        raise ValueError("R4 EDT payload size disagrees with its grid header")
    return resolution


def _rpy_quaternion(orientation):
    roll, pitch, yaw = (float(orientation[key]) / 2 for key in ("r", "p", "y"))
    sr, cr, sp, cp, sy, cy = (
        math.sin(roll),
        math.cos(roll),
        math.sin(pitch),
        math.cos(pitch),
        math.sin(yaw),
        math.cos(yaw),
    )
    return [
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
        cr * cp * cy + sr * sp * sy,
    ]


def _rotation_from_source(row, adf, text, volume):
    import yaml

    source = row["RotationSource"]
    if source == "fitted":
        return json.loads(row["FittedRotation"])
    if source == f"ADF/{adf.name}:current":
        return _rpy_quaternion(volume["location"]["orientation"])
    match = re.fullmatch(r"ADF/" + re.escape(adf.name) + r":commented:L(\d+)", source)
    if not match:
        raise ValueError("Unsupported historical rotation source")
    lines = text.splitlines()
    line_number = int(match[1])
    if line_number < 1 or line_number > len(lines):
        raise ValueError("Historical commented orientation line is unavailable")
    match = re.fullmatch(r"\s*#\s*orientation:\s*(\{[^}]+\})\s*", lines[line_number - 1])
    if match is None:
        raise ValueError("Historical commented orientation source changed")
    return _rpy_quaternion(yaml.safe_load(match[1]))


def _gui_edt_directory(entry, edt_root):
    """Resolve only the exact GUI-selected profile below an explicit EDT root."""
    value = str(entry.get("sdf_path", "")).strip().replace("\\", "/").strip("/")
    # Simulator GUI entries conventionally use /profile_name/. The leading slash
    # is simulator-root-relative, not an operating-system absolute path.
    relative = Path(value)
    if (
        not value
        or relative.is_absolute()
        or relative.drive
        or len(relative.parts) != 1
        or relative.name in {".", ".."}
    ):
        raise ValueError("GUI EDT selection must be one explicit relative profile directory")
    root = Path(edt_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Explicit EDT root must exist")
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError("GUI-selected EDT profile escapes the explicit EDT root")
    return result


def _trusted_edt_profile(specimen, edt_profile, resolution, hashes):
    """Require frozen model content identities, not just plausible file names."""
    path = Path(EDT_CATALOG_PATH).resolve()
    if not path.is_file():
        raise ValueError("Trusted EDT source catalogue is unavailable")
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest() != TRUSTED_EDT_CATALOG_SHA256:
        raise ValueError("EDT source catalogue is missing or differs from the trusted version")
    catalogue = json.loads(content)
    if (
        catalogue.get("schema") != "r4_edt_sources_v1"
        or catalogue.get("correction_table_sha256") not in TRUSTED_R4_CORRECTION_SHA256
    ):
        raise ValueError("EDT source catalogue is not bound to the trusted R4 correction authority")
    expected = catalogue.get("models", {}).get(specimen)
    if expected is None:
        raise ValueError(f"No trusted EDT source content is available for anatomy model {specimen}")
    if (
        expected.get("edt_profile") != edt_profile
        or expected.get("grid_resolution_xyz") != list(resolution)
        or expected.get("sha256") != hashes
    ):
        raise ValueError(f"EDT source content does not match the trusted anatomy model {specimen}")
    return path, digest


def validate_production_geometry(record, sources, *, project_root):
    """Return one verified profile snapshot before any production output exists."""
    profile = copy.deepcopy(load_anatomy_profile(project_root, record.anatomy_key))
    geometry = resolve_phase_geometry(
        profile, global_run_id=record.global_run_id, edt_profile=record.edt_profile
    )
    if geometry is not None and geometry["schema"] == MODEL_SCHEMA:
        from .anatomy_catalog import validate_model_geometry

        return validate_model_geometry(
            record, sources, profile, geometry, project_root=project_root
        )
    synthetic_markers = (
        profile.get("provenance") == SYNTHETIC_PROVENANCE
        and str(record.anatomy_mapping_status).casefold().startswith("synthetic")
        and str(record.source_status).casefold() in {"synthetic_fixture", "generated", "synthetic"}
    )
    historical = (
        re.fullmatch(r"P\d+_?T\d+", str(record.run_id), flags=re.I) is not None
        or re.search(r"(?:^|_)P\d+_?T\d+(?:$|_)", sources.raw_run_dir.name, flags=re.I) is not None
        or re.search(r"(?:^|_)P\d+_?T\d+(?:$|_)", Path(record.raw_run_relpath).name, flags=re.I)
        is not None
    )
    fixture_identity = str(record.dataset_id).casefold() in {"demo", "synthetic", "test_fixture"}
    if synthetic_markers and (historical or not fixture_identity):
        raise ValueError("Historical or real-dataset identities cannot use the synthetic exception")
    synthetic = synthetic_markers and fixture_identity and not historical
    if synthetic:
        if geometry is not None and geometry["schema"] != SYNTHETIC_SCHEMA:
            raise ValueError("A synthetic fixture cannot claim the real R4 authority")
        return GeometrySnapshot(
            profile,
            geometry,
            {
                "schema": "production_geometry_provenance_v1",
                "status": "explicit_synthetic_fixture",
                "global_run_id": record.global_run_id,
                "real_R4_validated": False,
                "historical_demo_geometry_exception": geometry is None,
                "user_review_performed": False,
                "clinician_review_performed": False,
            },
            {},
        )
    if geometry is None or geometry["schema"] != R4_SCHEMA:
        raise ValueError("Production real-data builds require source-verified R4 geometry")
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("R4 provenance validation requires PyYAML") from exc
    files = {}
    stats = {}

    def verified_file(path, expected, name):
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None:
            raise ValueError(f"R4 provenance requires SHA256: {name}")
        actual = _sha(path)
        if actual != expected.lower():
            raise ValueError(f"R4 source SHA256 mismatch: {name}")
        files[name] = {"path": str(path), "sha256": actual}
        state = path.stat()
        stats[str(path)] = (state.st_size, state.st_mtime_ns)
        return actual

    table = _path(geometry.get("source_correction_table"), project_root, "source_correction_table")
    table_hash = verified_file(table, geometry["correction_table_sha256"], "correction_table")
    if table_hash not in TRUSTED_R4_CORRECTION_SHA256:
        raise ValueError("Correction table is not the current trusted R4 v3 authority")
    with table.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rid = _recording_id(record.run_id)
    if _recording_id(geometry["calibration_recording_id"]) != rid:
        raise ValueError("R4 calibration belongs to a different recording")
    matches = [row for row in rows if row.get("RecordingID") == rid]
    if len(matches) != 1:
        raise ValueError("R4 correction row must be unique for this recording")
    row = matches[0]
    folder = row["FolderName"].replace(":", "_")
    if sources.raw_run_dir.resolve().name != folder or Path(record.raw_run_relpath).name != folder:
        raise ValueError("R4 source folder, registry and correction row disagree")
    for field, column in (
        ("actual_specimen", "ActualSpecimen"),
        ("rotation_source", "RotationSource"),
        ("geometry_quality", "ReviewStatus"),
    ):
        if geometry[field] != row[column]:
            raise ValueError(f"R4 authority mismatch: {column}")
    _quaternion(geometry["volume_quaternion_xyzw"], json.loads(row["UsedRotation"]), "UsedRotation")
    _close(geometry["recorder_scale_m_per_simulator_unit"], row["RecorderScale"], "RecorderScale")
    _close(geometry["edt_distance_scale_mm"], row["SpacingMm"], "SpacingMm")
    _close(profile.get("simulator_unit"), row["GUIUnit"], "GUIUnit")
    root = _path(
        geometry.get("source_simulator_root"), project_root, "source_simulator_root", directory=True
    )
    gui = _path(geometry.get("source_gui_path"), project_root, "source_gui_path")
    adf = _path(geometry.get("source_adf_path"), project_root, "source_adf_path")
    verified_file(gui, geometry.get("source_gui_sha256"), "source_gui")
    verified_file(adf, geometry.get("source_adf_sha256"), "source_adf")
    if _path(profile.get("adf_profile"), project_root, "adf_profile") != adf:
        raise ValueError("Profile and verified source ADF disagree")
    if not gui.is_relative_to(root) or not adf.is_relative_to(root):
        raise ValueError("GUI and ADF must belong to the explicit simulator source root")
    gui_document = yaml.safe_load(gui.read_text(encoding="utf-8"))
    entries = [gui_document[key] for key in gui_document["volumes"]]
    selected = [entry for entry in entries if (gui.parent / entry["adf_path"]).resolve() == adf]
    if len(selected) != 1 or adf.stem != "volume_256_" + row["ActualSpecimen"]:
        raise ValueError("GUI/ADF do not identify the corrected actual specimen")
    entry = selected[0]
    if str(entry["name"]).split()[-1].upper() != row["ActualAnatomy"].upper():
        raise ValueError("GUI anatomy and corrected anatomy disagree")
    _close(entry["unit"], row["GUIUnit"], "source GUIUnit")
    edt_root = _path(
        geometry.get("source_edt_root", str(root)), project_root, "source_edt_root", directory=True
    )
    expected_edt = _gui_edt_directory(entry, edt_root)
    if expected_edt != sources.edt_dir.resolve() or expected_edt.name != record.edt_profile:
        raise ValueError("Actual EDT directory is not the corrected GUI-selected specimen")
    declared_hashes = geometry.get("source_edt_sha256")
    expected_names = {stem + ".edt" for stem in REQUIRED_EDT_STEMS}
    if not isinstance(declared_hashes, dict) or set(declared_hashes) != expected_names:
        raise ValueError("R4 provenance requires all 16 EDT source hashes")
    resolutions = []
    for name in sorted(expected_names):
        path = expected_edt / name
        verified_file(path, declared_hashes[name], name)
        resolutions.append(_header(path))
    resolution = resolutions[0]
    if len(set(resolutions)) != 1 or tuple(geometry.get("grid_resolution_xyz", ())) != resolution:
        raise ValueError("R4 anatomy grids and declared resolution disagree")
    catalogue, catalogue_digest = _trusted_edt_profile(
        row["ActualSpecimen"],
        expected_edt.name,
        resolution,
        {name: files[name]["sha256"] for name in sorted(expected_names)},
    )
    verified_file(catalogue, catalogue_digest, "edt_source_catalogue")
    adf_text = adf.read_text(encoding="utf-8")
    adf_document = yaml.safe_load(adf_text)
    volume = adf_document[adf_document["volumes"][0]]
    dimensions = [float(volume["dimensions"][axis]) for axis in "xyz"]
    for actual, expected in zip(geometry["volume_dimensions_sim"], dimensions, strict=True):
        _close(actual, expected, "source ADF dimensions")
    _close(
        dimensions[0] / (resolution[0] * float(entry["unit"])),
        row["SpacingMm"],
        "dimension/grid/GUI spacing",
    )
    expected_quaternion = _rotation_from_source(row, adf, adf_text, volume)
    _quaternion(geometry["volume_quaternion_xyzw"], expected_quaternion, "rotation source")
    if profile.get("config_path"):
        config_path = Path(profile["config_path"])
        verified_file(config_path, _sha(config_path), "profile_configuration")
    manifest = {
        "schema": "production_geometry_provenance_v1",
        "status": "verified_R4_v3_sources",
        "global_run_id": record.global_run_id,
        "recording_id": rid,
        "real_R4_validated": True,
        "actual_specimen": row["ActualSpecimen"],
        "geometry_quality": row["ReviewStatus"],
        "source_files": files,
        "grid_resolution_xyz": list(resolution),
        "geometry_contract": geometry,
        "correction_table_rows": len(rows),
        "correction_quality_counts": dict(Counter(item["ReviewStatus"] for item in rows)),
        "scientific_all_recording_geometry": False,
        "user_review_performed": False,
        "clinician_review_performed": False,
        "scope": (
            "Source identity and R4 numerical contract; no new calibration or clinical validation"
        ),
        "native_quantity_policy": (
            "Preserve recorded world-metre motion, camera calibration, depth units "
            "and metadata/voxel_volume; these are not EDT millimetres"
        ),
    }
    return GeometrySnapshot(profile, geometry, manifest, stats)


def assert_geometry_sources_unchanged(snapshot):
    """Fail if a verified source was replaced during this build."""
    for name, expected in snapshot.source_stats.items():
        state = Path(name).stat()
        if (state.st_size, state.st_mtime_ns) != expected:
            raise ValueError("A verified R4 source changed during the build")
