"""Generate run configuration and evidence from recording directories.

One directory is one recording; sibling HDF5 files are its chunks. Runtime
locations are installed once. Source recordings and previous results are read-only.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .project import PROJECT_ROOT
from .registry import REGISTRY_FIELDS, RunRecord
from .source_config import ResolvedRunSources


def discover_recordings(input_dir: Path) -> list[Path]:
    """Discover direct chunk parents, never concatenate separate directories."""
    root = Path(input_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"HDF5 input directory is unavailable: {root}")
    directories = {
        path.parent.resolve()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".hdf5", ".h5"}
    }
    if any(not path.is_relative_to(root) for path in directories):
        raise ValueError("Recording directories must remain inside the input root")
    if not directories:
        raise ValueError(f"No HDF5 recordings found in {root}")
    return sorted(directories, key=lambda path: str(path).casefold())


def run_id_for_directory(path: Path) -> str:
    """Keep explicit historical IDs; otherwise derive a stable local identity."""
    path = Path(path).expanduser().resolve()
    match = re.search(r"(?:^|_)P0*(\d+)_?T0*(\d+)$", path.name, re.I)
    if match:
        return f"P{int(match[1])}T{int(match[2])}"
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", path.name).strip("_-")[:60] or "recording"
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
    return f"{name}_{digest}"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_directory_runtime(path: Path | None, *, project_root: Path = PROJECT_ROOT) -> dict:
    """Load the once-per-installation locations, never a per-case path map."""
    path = Path(path or project_root / "config.local.json").expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f"Install the local HDF5 runtime configuration once: {path}. "
            "Copy config.example.json to config.local.json and edit its paths."
        )
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("schema") != "hdf5_directory_runtime_v1":
        raise ValueError("Unsupported HDF5 directory runtime configuration")
    result = {"configuration_path": path}
    for key in ("simulator_root", "edt_root", "correction_table", "phase_model_bundle"):
        value = payload.get(key)
        if key == "edt_root" and value is None:
            value = payload.get("simulator_root")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Runtime configuration requires {key}")
        item = Path(value).expanduser()
        item = (item if item.is_absolute() else project_root / item).resolve()
        directory = key in {"simulator_root", "edt_root", "phase_model_bundle"}
        if not (item.is_dir() if directory else item.is_file()):
            raise FileNotFoundError(f"Runtime {key} is unavailable: {item}")
        result[key] = item
    fps = float(payload.get("video_fps", 10.0))
    if not math.isfinite(fps) or not 0 < fps <= 60:
        raise ValueError("video_fps must be finite and in (0, 60]")
    result["video_fps"] = fps
    reference = payload.get("motion_reference")
    if reference:
        item = Path(reference).expanduser()
        item = (item if item.is_absolute() else project_root / item).resolve()
        if not item.is_file():
            raise FileNotFoundError(f"Runtime motion_reference is unavailable: {item}")
        from .motion_comparison import validate_expert_reference

        validate_expert_reference(json.loads(item.read_text(encoding="utf-8")))
        result["motion_reference"] = item
    return result


@contextmanager
def _anatomy_configuration(path: Path):
    previous = os.environ.get("MASTOID_ANATOMY_PROFILES")
    os.environ["MASTOID_ANATOMY_PROFILES"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MASTOID_ANATOMY_PROFILES", None)
        else:
            os.environ["MASTOID_ANATOMY_PROFILES"] = previous


def _record(raw: Path, run_id: str, anatomy, project_root: Path) -> RunRecord:
    identity = re.fullmatch(r"P(\d+)T(\d+)", run_id)
    row = dict.fromkeys(REGISTRY_FIELDS, "")
    row.update(
        dataset_id="HDF5",
        run_id=run_id,
        participant_id=f"P{identity[1]}" if identity else "",
        trial_id=f"T{identity[2]}" if identity else "",
        anatomy_key=anatomy.anatomy_key,
        anatomy_mapping_status=(
            "configured_r4_identity"
            if anatomy.metadata.get("mode") == "historical_r4_recording"
            else "configured_model_sources_unverified_recording"
        ),
        anatomy_mapping_basis=(
            "Automatically resolved from verified anatomy sources; see anatomy_resolution.json"
        ),
        raw_run_relpath=str(raw),
        edt_profile=anatomy.edt_dir.name,
        source_status="local_read_only_hdf5",
        external_case_id=f"HDF5:{run_id}",
        golden_reference_path=project_root / "validation/not-configured.json",
    )
    return RunRecord(**row)


def _prepare_case(raw, run_id, case_dir, runtime, anatomy_model, project_root):
    from .anatomy_catalog import configure_anatomy
    from .hdf5_clock import generate_hdf5_alignment

    anatomy = configure_anatomy(
        simulator_root=runtime["simulator_root"],
        correction_table=runtime["correction_table"],
        edt_root=runtime.get("edt_root", runtime["simulator_root"]),
        recording_dir=raw,
        run_id=run_id,
        dataset_id="HDF5",
        anatomy_model=anatomy_model,
    )
    record = _record(raw, run_id, anatomy, project_root)
    config_dir = case_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=False)
    clock = generate_hdf5_alignment(raw, case_dir / "clock", fps=runtime["video_fps"])
    if clock["duration_s"] < 1.0:
        raise ValueError(
            "Recording must span at least one second for the phase model; no duration is fabricated"
        )
    profile_path = config_dir / "anatomy_profiles.local.json"
    _write_json(
        profile_path,
        {"schema": "anatomy_profiles_v1", "profiles": {anatomy.anatomy_key: anatomy.profile}},
    )
    _write_json(config_dir / "anatomy_resolution.json", anatomy.metadata)
    sources = ResolvedRunSources(
        raw_run_dir=raw,
        edt_dir=anatomy.edt_dir,
        phase_annotation_file=None,
        golden_reference_path=record.golden_reference_path,
        phase_model_bundle=runtime["phase_model_bundle"],
        frame_alignment_file=Path(clock["alignment_path"]),
        phase_duration_s=clock["duration_s"],
    )
    _write_json(
        config_dir / "source_paths.local.json",
        {
            "schema_version": "source_paths_v1",
            "data_root": str(raw),
            "edt_root": str(anatomy.edt_dir.parent),
            "phase_annotation_file": None,
            "phase_model_bundle": str(sources.phase_model_bundle),
            "frame_alignment_file": str(sources.frame_alignment_file),
            "phase_duration_s": sources.phase_duration_s,
        },
    )
    with (config_dir / "runs.local.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REGISTRY_FIELDS)
        writer.writeheader()
        writer.writerow({key: str(getattr(record, key)) for key in REGISTRY_FIELDS})
    return record, sources, profile_path, anatomy.metadata, clock


def process_hdf5_directory(
    input_dir: Path,
    *,
    anatomy_model: str = "auto",
    runtime_config: Path | None = None,
    output_dir: Path | None = None,
    project_root: Path = PROJECT_ROOT,
    prepare_only: bool = False,
    base_port: int = 8810,
) -> dict:
    """Prepare or build all discovered recordings; preserve each case failure."""
    from .pipeline import build_evidence

    project_root = Path(project_root).expanduser().resolve()
    input_dir = Path(input_dir).expanduser().resolve()
    raw_directories = discover_recordings(input_dir)
    identified = [(raw, run_id_for_directory(raw)) for raw in raw_directories]
    identifiers = [run_id for _, run_id in identified]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Duplicate recording IDs in input; keep one source folder per recording")
    if not 1 <= base_port <= 65535 or base_port + len(identified) - 1 > 65535:
        raise ValueError("Batch ports must remain in 1..65535")
    if output_dir is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_dir = project_root / "results/hdf5_batches" / f"{stamp}_{uuid4().hex[:8]}"
    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.is_relative_to(input_dir) or input_dir.is_relative_to(output_dir):
        raise ValueError("Output directory and input directory must be separate and not nested")
    if output_dir.exists():
        raise FileExistsError(
            f"Use a new output directory; existing results are preserved: {output_dir}"
        )
    runtime = load_directory_runtime(runtime_config, project_root=project_root)
    output_dir.mkdir(parents=True)
    summary = {
        "schema": "hdf5_directory_build_v1",
        "input_directory": str(input_dir),
        "output_directory": str(output_dir),
        "anatomy_model_selection": anatomy_model,
        "runtime_configuration": str(runtime["configuration_path"]),
        "clock_policy": "native_hdf5_elapsed_time_v1",
        "prepare_only": prepare_only,
        "recording_count": len(identified),
        "cases": [],
        "user_review_performed": False,
        "clinician_review_performed": False,
    }
    manifest = {
        "schema_version": "study_manifest_v1",
        "app_mode": "recommendations",
        "interface_version": "expert-feedback-v1",
        "host": "127.0.0.1",
        "base_port": base_port,
        "ui_directory": "apps/recommendations",
        "data_access": {"expert_annotations": "none", "participant_responses": "none"},
        "cases": [],
    }
    for ordinal, (raw, run_id) in enumerate(identified):
        case_dir = output_dir / run_id
        item = {"run_id": run_id, "input_directory": str(raw), "result_directory": str(case_dir)}
        try:
            record, sources, profile_path, anatomy_metadata, clock = _prepare_case(
                raw,
                run_id,
                case_dir,
                runtime,
                anatomy_model,
                project_root,
            )
            item.update(anatomy=anatomy_metadata, alignment=clock)
            with _anatomy_configuration(profile_path):
                if prepare_only:
                    from .r4_provenance import validate_production_geometry
                    from .source_audit import build_source_audit

                    geometry = validate_production_geometry(
                        record, sources, project_root=project_root
                    )
                    audit = build_source_audit(record, sources, hash_hdf5=False)
                    if not audit["ready_for_frame_extraction"]:
                        _write_json(
                            case_dir / "preflight.json",
                            {"geometry": geometry.manifest, "source_audit": audit},
                        )
                        raise ValueError(
                            "Source audit failed: " + ", ".join(audit["blocking_items"])
                        )
                    if audit.get("hdf5", {}).get("format") == "v3":
                        from .frame_pipeline import extract_core_frames
                        from .frame_pipeline.phase_geometry import validate_v3_volume_contract

                        core = extract_core_frames(record, sources, project_root=project_root)
                        geometry.manifest["v3_volume_contract"] = validate_v3_volume_contract(
                            core.frames,
                            geometry.profile,
                            raw_run_dir=sources.raw_run_dir,
                        )
                    _write_json(
                        case_dir / "preflight.json",
                        {"geometry": geometry.manifest, "source_audit": audit},
                    )
                    item["status"] = "prepared"
                else:
                    build = build_evidence(
                        record=record,
                        sources=sources,
                        result_dir=case_dir,
                        project_root=project_root,
                        build_video=True,
                        video_fps=runtime["video_fps"],
                    )
                    item.update(status="completed", build=build)
                    manifest["cases"].append(
                        {
                            "case_id": run_id,
                            "run_id": run_id,
                            "display_name": run_id,
                            "port": base_port + ordinal,
                            "evidence_database": build["evidence_database"],
                            "review_video": build["review_video"],
                            "review_video_manifest": str(case_dir / "review_video_manifest.json"),
                            "ray_v3_directory": str(case_dir / "stages/08_under_ledge_ray"),
                            **(
                                {
                                    "cohort_benchmark": str(runtime["motion_reference"]),
                                    "cohort_benchmark_sha256": hashlib.sha256(
                                        runtime["motion_reference"].read_bytes()
                                    ).hexdigest(),
                                }
                                if runtime.get("motion_reference")
                                else {}
                            ),
                        }
                    )
        except Exception as exc:
            item.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        summary["cases"].append(item)
        summary["failed_count"] = sum(row["status"] == "failed" for row in summary["cases"])
        summary["completed_count"] = sum(row["status"] == "completed" for row in summary["cases"])
        summary["prepared_count"] = sum(row["status"] == "prepared" for row in summary["cases"])
        _write_json(output_dir / "batch_summary.json", summary)
        print(
            f"{run_id}: {item['status']}"
            + (f" ({item['error']})" if item["status"] == "failed" else ""),
            flush=True,
        )
    summary["status"] = (
        "failed"
        if summary["failed_count"] == len(identified)
        else (
            "partial_failure"
            if summary["failed_count"]
            else "prepared"
            if prepare_only
            else "completed"
        )
    )
    if manifest["cases"]:
        manifest_path = output_dir / "recommendations_manifest.json"
        _write_json(manifest_path, manifest)
        summary["recommendations_manifest"] = str(manifest_path)
    _write_json(output_dir / "batch_summary.json", summary)
    return summary
