"""Minimal command line interface for evidence builds and local web apps."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .pipeline import build_evidence
from .project import OUTPUT_ROOT, PROJECT_ROOT, REGISTRY_PATH
from .registry import get_run, load_registry
from .source_config import (
    SourceRoots,
    load_source_roots,
    resolve_run_sources,
)
from .web import APP_MODES, serve_feedback_app, serve_intake_app


def _path(value: str | Path, *, base: Path = PROJECT_ROOT) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _configured_motion_reference() -> Path | None:
    """One editable deployment path; old installations need no new setting."""
    config = PROJECT_ROOT / "config.local.json"
    if not config.is_file():
        return None
    value = json.loads(config.read_text(encoding="utf-8-sig")).get("motion_reference")
    if not value:
        return None
    reference = _path(value)
    if not reference.is_file():
        raise FileNotFoundError(f"Configured motion_reference is unavailable: {reference}")
    return reference


def _configured_phase_reference() -> Path | None:
    """The bundled whole-phase reference needs no expert source data at serve time."""
    config = PROJECT_ROOT / "config.local.json"
    values = json.loads(config.read_text(encoding="utf-8-sig")) if config.is_file() else {}
    value = values.get("phase_summary_reference", "references/r4_phase_summary_v1.json")
    if not value:
        return None
    reference = _path(value)
    if not reference.is_file():
        raise FileNotFoundError(f"Configured phase_summary_reference is unavailable: {reference}")
    return reference


TRUSTED_R4_CATALOG_SHA256 = "99fc284bef5e3cbf843be99da1ce5678df735a20257adbf89f02103f04304e7a"


def build_motion_reference(catalog: Path, output: Path) -> dict[str, Any]:
    """Build a portable reference from frozen R4 inputs, never feedback text."""
    from .motion_comparison import (
        add_stroke_frequency,
        add_stroke_length,
        build_expert_reference,
        build_motion_windows,
        write_motion_artifact,
    )

    catalog, output = Path(catalog).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Use a new reference filename; existing references are preserved")
    if output.is_relative_to(catalog):
        raise ValueError("Reference output must remain outside the append-only source catalog")
    manifest_path = catalog / "releases/R4_20261029/manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_hash != TRUSTED_R4_CATALOG_SHA256:
        raise ValueError("R4 catalog manifest does not match the verified published release")
    manifest = json.loads(manifest_bytes)
    hashes = {row["Path"]: row["SHA256"] for row in manifest["Files"]}

    def verified_csv(relative: str):
        path = (catalog / relative).resolve()
        if not path.is_relative_to(catalog):
            raise ValueError("Catalog source escaped its root")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != hashes.get(relative):
            raise ValueError(f"R4 source checksum mismatch: {relative}")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        return rows, {"path": str(path), "sha256": digest}

    participants, people_source = verified_csv("participants.csv")
    recordings, recordings_source = verified_csv("recordings.csv")
    experts = {row["ParticipantID"] for row in participants if float(row["TrainingYears"]) >= 11}
    builds, excluded = [], []
    for record in recordings:
        if record["ParticipantID"] not in experts:
            continue
        rid = record["RecordingID"]
        if record["PhaseStatus"] != "complete":
            excluded.append({"recording_id": rid, "reason": "incomplete_manual_phase_boundaries"})
            continue
        relative = record["RawFolder"].replace("\\", "/")
        frames, frames_source = verified_csv(
            relative + "/frame_level_spatial_all_frames_visibility.csv"
        )
        alignment, phase_source = verified_csv(relative + "/alignment.csv")
        if len(frames) != len(alignment) or len(frames) != int(record["FrameRows"]):
            raise ValueError(f"R4 frame/alignment inventory mismatch: {rid}")
        first_removal = next(
            (i for i, row in enumerate(frames) if float(row["frame_removed_voxels"] or 0) > 0),
            len(frames),
        )
        origin = float(frames[0]["t_abs"])
        previous, interval = None, 0
        for i, (row, aligned) in enumerate(zip(frames, alignment, strict=True)):
            if int(row["frame_idx_global"]) != int(aligned["frame_idx_global"]):
                raise ValueError(f"R4 phase row identity mismatch: {rid}")
            label = aligned["phase"]
            phase = (
                "pre_drilling"
                if i < first_removal
                else {
                    "pre_antrum": "early_surface",
                    "antrum": "antrum",
                    "incus": "incus",
                    "post_incus": "incus",
                }.get(label)
            )
            if phase is None:
                raise ValueError(f"Unknown R4 reference phase: {rid}: {label}")
            if phase != previous:
                interval += 1
            row.update(
                t_video_s=float(row["t_abs"]) - origin,
                phase=phase,
                phase_id=f"R4:{rid}:phase:{interval}",
            )
            previous = phase
        builds.append(
            add_stroke_length(
                add_stroke_frequency(
                    build_motion_windows(
                        frames,
                        global_run_id=f"R4:{rid}",
                        participant_id=record["ParticipantID"],
                        provenance={
                            "pose_source": "canonical_vision_pose",
                            "position_unit": "m",
                            "phase_source_kind": "human_phase_annotation",
                            "clock_source": "native_elapsed_from_R4_frame_t_abs",
                            "phase_alignment_policy": (
                                "manual_world_video_phase_joined_by_frame_identity_then_native_windows"
                            ),
                            "recording_id": rid,
                            "actual_specimen": record["ActualSpecimen"],
                            "assistance_condition": record["AssistanceCondition"],
                            "geometry_needs_review": rid
                            in manifest.get("GeometryNeedsReviewRecordingIDs", []),
                            "source_artifacts": [
                                dict(frames_source, kind="frames"),
                                dict(phase_source, kind="phases"),
                            ],
                        },
                    ),
                    frames,
                ),
                frames,
            )
        )
    reference = build_expert_reference(
        builds,
        expert_participant_ids=sorted(experts),
        provenance={
            "expert_definition": (
                "Published participant metadata TrainingYears >= 11; "
                "expertise is not a local quality label"
            ),
            "release": "R4_20261029",
            "release_manifest_sha256": manifest_hash,
            "source_artifacts": [
                dict(people_source, kind="participants"),
                dict(recordings_source, kind="recordings"),
            ],
            "excluded_recordings": excluded,
            "anatomy_matching": "pooled_specimens",
            "assistance_matching": "pooled_conditions",
            "geometry_needs_review_policy": "retained",
            "geometry_scientific_acceptance_status": manifest.get(
                "GeometryScientificAcceptanceStatus"
            ),
            "user_review_performed": False,
            "clinician_review_performed": False,
        },
    )
    write_motion_artifact(output, reference)
    return {
        "output": str(output),
        "reference_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "expert_participant_ids": sorted(experts),
        "recording_count": len(builds),
        "excluded_recordings": excluded,
    }


def motion_reference_command(args: argparse.Namespace) -> int:
    print(json.dumps(build_motion_reference(_path(args.catalog), _path(args.output)), indent=2))
    return 0


def phase_reference_command(args: argparse.Namespace) -> int:
    from .phase_summary import build_phase_reference_from_motion_reference

    result = build_phase_reference_from_motion_reference(
        _path(args.motion_reference), _path(args.output)
    )
    print(json.dumps(result, indent=2))
    return 0


def build_command(args: argparse.Namespace) -> int:
    if args.anatomy_profiles:
        os.environ["MASTOID_ANATOMY_PROFILES"] = str(_path(args.anatomy_profiles))
    registry_path = _path(args.registry)
    record = get_run(
        args.run_id,
        load_registry(registry_path, project_root=PROJECT_ROOT),
    )
    direct = (args.raw_run_dir, args.edt_dir)
    if any(direct) and not all(direct):
        raise ValueError("--raw-run-dir and --edt-dir must be provided together")
    if all(direct):
        if not args.phase_model_bundle and not args.phase_annotation_file:
            raise ValueError(
                "Direct sources require --phase-model-bundle or --phase-annotation-file"
            )
        roots = SourceRoots(
            data_root=_path(args.raw_run_dir),
            edt_root=_path(args.edt_dir),
            phase_annotation_file=_path(args.phase_annotation_file)
            if args.phase_annotation_file
            else None,
            phase_model_bundle=_path(args.phase_model_bundle) if args.phase_model_bundle else None,
            frame_alignment_file=_path(args.frame_alignment_file)
            if args.frame_alignment_file
            else None,
            phase_duration_s=args.phase_duration_s,
        )
        sources = resolve_run_sources(
            record,
            roots,
            raw_run_dir=roots.data_root,
            edt_dir=roots.edt_root,
            phase_annotation_file=roots.phase_annotation_file,
        )
    else:
        roots = load_source_roots(
            _path(args.source_config) if args.source_config else None,
            project_root=PROJECT_ROOT,
            phase_model_bundle=_path(args.phase_model_bundle) if args.phase_model_bundle else None,
        )
        sources = resolve_run_sources(
            record,
            roots,
            phase_annotation_file=_path(args.phase_annotation_file)
            if args.phase_annotation_file
            else None,
            phase_model_bundle=_path(args.phase_model_bundle) if args.phase_model_bundle else None,
            frame_alignment_file=_path(args.frame_alignment_file)
            if args.frame_alignment_file
            else None,
            phase_duration_s=args.phase_duration_s,
        )
    output_root = _path(args.output_root)
    summary = build_evidence(
        record=record,
        sources=sources,
        result_dir=output_root / record.run_id,
        project_root=PROJECT_ROOT,
        build_video=args.build_video,
        video_fps=args.video_fps,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mode = str(payload.get("app_mode") or "")
    if mode not in APP_MODES:
        raise ValueError("Manifest app_mode must be recommendations or expert-study")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Manifest must contain at least one case")
    access = dict(payload.get("data_access") or {})
    if mode == "recommendations":
        if access.get("expert_annotations") != "none":
            raise ValueError("Recommendations manifest must disable expert annotations")
        if access.get("participant_responses") != "none":
            raise ValueError("Recommendations manifest must disable participant responses")
        if any("study_state" in case for case in cases):
            raise ValueError("Recommendations cases must not contain study_state paths")
    else:
        if access.get("expert_annotations") != "read_write":
            raise ValueError("Expert-study manifest must enable expert annotations")
        if access.get("participant_responses") != "read_write":
            raise ValueError("Expert-study manifest must enable participant responses")
        if any(not str(case.get("study_state") or "").strip() for case in cases):
            raise ValueError("Every expert-study case requires study_state")
    return payload


def _manifest_serve_options(
    args: argparse.Namespace,
) -> dict[str, Any]:
    manifest_path = _path(args.manifest)
    payload = _load_manifest(manifest_path)
    mode = str(payload["app_mode"])
    cases = [dict(row) for row in payload["cases"]]
    selected = next(
        (
            row
            for row in cases
            if args.case_id
            in {
                None,
                str(row.get("case_id") or ""),
                str(row.get("run_id") or ""),
            }
        ),
        None,
    )
    if selected is None:
        raise KeyError(f"Case not found in manifest: {args.case_id}")
    host = args.host or str(payload.get("host") or "127.0.0.1")
    port = args.port or int(selected.get("port") or payload.get("base_port") or 0)
    expected_ui = {
        "recommendations": PROJECT_ROOT / "apps" / "recommendations",
        "expert-study": PROJECT_ROOT / "apps" / "expert_study",
    }[mode].resolve()
    ui_dir = _path(str(payload.get("ui_directory") or ""))
    if ui_dir != expected_ui:
        raise ValueError(f"{mode} mode must use {expected_ui}; got {ui_dir}")
    available_runs = [
        {
            "run_id": str(row["run_id"]),
            "url": f"http://{host}:{int(row.get('port') or payload.get('base_port') or 0)}/",
        }
        for row in cases
    ]
    database = _path(str(selected["evidence_database"]))
    case_root = database.parent
    ray_v3 = (
        _path(str(selected["ray_v3_directory"]))
        if selected.get("ray_v3_directory")
        else case_root / "ray_v3"
    )
    cohort = (
        _path(str(selected["cohort_benchmark"]))
        if selected.get("cohort_benchmark")
        else case_root / "cohort_benchmark.json"
    )
    if selected.get("cohort_benchmark") and not cohort.is_file():
        raise FileNotFoundError(f"Case reference is unavailable: {cohort}")
    expected_reference_hash = selected.get("cohort_benchmark_sha256")
    if expected_reference_hash and (
        not cohort.is_file()
        or hashlib.sha256(cohort.read_bytes()).hexdigest() != expected_reference_hash
    ):
        raise ValueError("Case reference no longer matches its batch manifest")
    return {
        "mode": mode,
        "db_path": database,
        "run_id": str(selected["run_id"]),
        "ui_dir": ui_dir,
        "host": host,
        "port": port,
        "video_path": _path(str(selected["review_video"])),
        "video_manifest_path": _path(str(selected["review_video_manifest"])),
        "ray_v3_dir": ray_v3 if ray_v3.is_dir() else None,
        "cohort_benchmark_path": cohort if cohort.is_file() else _configured_motion_reference(),
        "phase_reference_path": _configured_phase_reference(),
        "study_state_path": (
            _path(str(selected["study_state"])) if mode == "expert-study" else None
        ),
        "available_runs": available_runs,
        "display_name": str(selected.get("display_name") or selected.get("case_id") or "Case"),
        "interface_version": str(payload.get("interface_version") or "expert-feedback-v1"),
    }


def _direct_serve_options(args: argparse.Namespace) -> dict[str, Any]:
    required = {
        "--mode": args.mode,
        "--run-id": args.run_id,
        "--db-path": args.db_path,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("Direct serve mode requires " + ", ".join(missing))
    mode = str(args.mode)
    expected_ui = (
        PROJECT_ROOT / "apps" / ("recommendations" if mode == "recommendations" else "expert_study")
    ).resolve()
    ui_dir = _path(args.ui_dir) if args.ui_dir else expected_ui
    if ui_dir != expected_ui:
        raise ValueError(f"{mode} mode must use {expected_ui}; got {ui_dir}")
    state = _path(args.study_state) if args.study_state else None
    if mode == "recommendations" and state is not None:
        raise ValueError("Recommendations mode cannot use --study-state")
    if mode == "expert-study" and state is None:
        raise ValueError("Expert-study mode requires --study-state")
    return {
        "mode": mode,
        "db_path": _path(args.db_path),
        "run_id": str(args.run_id),
        "ui_dir": ui_dir,
        "host": args.host or "127.0.0.1",
        "port": args.port or (8785 if mode == "recommendations" else 8790),
        "video_path": _path(args.video) if args.video else None,
        "video_manifest_path": (_path(args.video_manifest) if args.video_manifest else None),
        "ray_v3_dir": _path(args.ray_v3_dir) if args.ray_v3_dir else None,
        "cohort_benchmark_path": (
            _path(args.cohort_benchmark)
            if args.cohort_benchmark
            else _configured_motion_reference()
        ),
        "phase_reference_path": _configured_phase_reference(),
        "study_state_path": state,
        "available_runs": [{"run_id": str(args.run_id), "url": ""}],
        "display_name": args.display_name or "Case 01",
        "interface_version": args.interface_version or "expert-feedback-v1",
    }


def serve_intake_command(args: argparse.Namespace) -> int:
    """Background questionnaire only, while process-hdf5 runs (FIVRS resident study)."""
    serve_intake_app(
        study_state_path=_path(args.study_state),
        ui_dir=(PROJECT_ROOT / "apps" / "expert_study").resolve(),
        host=args.host or "127.0.0.1",
        port=args.port or 8790,
        display_name=args.display_name or "Case 01",
        interface_version=args.interface_version or "expert-feedback-v1",
    )
    return 0


def serve_command(args: argparse.Namespace) -> int:
    options = _manifest_serve_options(args) if args.manifest else _direct_serve_options(args)
    serve_feedback_app(**options)
    return 0


def migrate_study_state_command(args: argparse.Namespace) -> int:
    from .study_storage import migrate_study_state

    receipt = migrate_study_state(_path(args.source), _path(args.output_dir))
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0


def process_hdf5_command(args: argparse.Namespace) -> int:
    from .directory_pipeline import process_hdf5_directory

    if args.prepare_only and args.serve:
        raise ValueError("--serve requires a completed build, not --prepare-only")
    summary = process_hdf5_directory(
        _path(args.input_dir),
        anatomy_model=args.anatomy_model,
        runtime_config=_path(args.runtime_config) if args.runtime_config else None,
        output_dir=_path(args.output_dir) if args.output_dir else None,
        prepare_only=args.prepare_only,
        base_port=args.base_port,
    )
    print(f"Batch status: {summary['status']}; failed recordings: {summary['failed_count']}")
    print(f"Results: {summary['output_directory']}")
    if summary.get("recommendations_manifest"):
        print(f"Feedback manifest: {summary['recommendations_manifest']}")
    serve_status = 0
    if args.serve and summary.get("recommendations_manifest"):
        serve_status = serve_batch_command(
            argparse.Namespace(manifest=Path(summary["recommendations_manifest"]))
        )
    return serve_status or (1 if summary["failed_count"] else 0)


def serve_batch_command(args: argparse.Namespace) -> int:
    """Start the unchanged per-case application on its declared local ports."""
    import socket
    import subprocess
    import sys
    import time

    manifest_path = _path(args.manifest)
    manifest = _load_manifest(manifest_path)
    if manifest["app_mode"] != "recommendations":
        raise ValueError("Batch serving supports the read-only recommendations application")
    cases = manifest["cases"]
    identifiers = [str(row["case_id"]) for row in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Batch case IDs must be unique")
    host = str(manifest.get("host") or "127.0.0.1")
    ports = [int(row.get("port") or manifest.get("base_port") or 0) for row in cases]
    if len(ports) != len(set(ports)) or any(not 1 <= port <= 65535 for port in ports):
        raise ValueError("Batch cases require distinct ports in 1..65535")
    # Detect occupied ports before launching any child; never terminate an
    # existing application to reclaim a port.
    for port in ports:
        with socket.socket() as probe:
            probe.bind((host, port))
    children = []
    try:
        for row in cases:
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(PROJECT_ROOT / "run.py"),
                        "serve",
                        "--manifest",
                        str(manifest_path),
                        "--case-id",
                        str(row["case_id"]),
                    ],
                    cwd=str(PROJECT_ROOT),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            )
        print(f"Feedback batch: http://{host}:{ports[0]}/ (Ctrl+C stops this batch)", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.25)
        return next((child.returncode or 1 for child in children if child.poll() is not None), 1)
    except KeyboardInterrupt:
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="Build evidence and SQLite from one registered HDF5 run"
    )
    build.add_argument("--run-id", required=True)
    build.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    build.add_argument("--source-config", type=Path)
    build.add_argument("--raw-run-dir", type=Path)
    build.add_argument("--edt-dir", type=Path)
    build.add_argument("--phase-annotation-file", type=Path)
    build.add_argument(
        "--phase-model-bundle",
        type=Path,
        help="Use the frozen offline signal phase model instead of manual boundaries",
    )
    build.add_argument(
        "--frame-alignment-file",
        type=Path,
        help="Explicit frame-index to video-time alignment CSV; no phase labels",
    )
    build.add_argument(
        "--phase-duration-s",
        type=float,
        help="Explicit recording/video duration used for the phase endpoint grid",
    )
    build.add_argument("--anatomy-profiles", type=Path)
    build.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    build.add_argument("--build-video", action="store_true")
    build.add_argument("--video-fps", type=float, default=10.0)
    build.set_defaults(handler=build_command)

    directory = subparsers.add_parser(
        "process-hdf5", help="Build video, phases and feedback from HDF5 recording folders"
    )
    directory.add_argument("--input-dir", type=Path, required=True)
    directory.add_argument(
        "--anatomy-model",
        default="auto",
        help=(
            "Actual specimen (for example LT151); auto uses R4 identity or metadata.json volume_adf"
        ),
    )
    directory.add_argument(
        "--runtime-config", type=Path, help="Once-per-installation model/simulator locations"
    )
    directory.add_argument(
        "--output-dir",
        type=Path,
        help="New output directory; default is a versioned results folder",
    )
    directory.add_argument(
        "--prepare-only",
        action="store_true",
        help="Generate and validate inputs without inference/video",
    )
    directory.add_argument("--base-port", type=int, default=8810)
    directory.add_argument(
        "--serve",
        action="store_true",
        help="Open local services for all successful cases after building",
    )
    directory.set_defaults(handler=process_hdf5_command)

    serve_batch = subparsers.add_parser(
        "serve-batch", help="Serve every completed case in a generated batch"
    )
    serve_batch.add_argument("--manifest", type=Path, required=True)
    serve_batch.set_defaults(handler=serve_batch_command)

    reference = subparsers.add_parser(
        "build-motion-reference",
        help="Build a portable five-second reference from the verified R4 catalog",
    )
    reference.add_argument("--catalog", type=Path, required=True)
    reference.add_argument("--output", type=Path, required=True)
    reference.set_defaults(handler=motion_reference_command)

    phase_reference = subparsers.add_parser(
        "build-phase-reference",
        help="Build a whole-phase reference from the saved cohort's verified original signals",
    )
    phase_reference.add_argument(
        "--motion-reference", type=Path, default=Path("references/r4_motion_windows_v6.json")
    )
    phase_reference.add_argument("--output", type=Path, required=True)
    phase_reference.set_defaults(handler=phase_reference_command)

    serve = subparsers.add_parser(
        "serve", help="Serve the read-only recommendations or expert-study app"
    )
    serve.add_argument("--manifest", type=Path)
    serve.add_argument("--case-id")
    serve.add_argument("--mode", choices=sorted(APP_MODES))
    serve.add_argument("--run-id")
    serve.add_argument("--db-path", type=Path)
    serve.add_argument("--ui-dir", type=Path)
    serve.add_argument("--video", type=Path)
    serve.add_argument("--video-manifest", type=Path)
    serve.add_argument("--ray-v3-dir", type=Path)
    serve.add_argument("--cohort-benchmark", type=Path)
    serve.add_argument(
        "--study-state", type=Path,
        help=(
            "Participant JSON directory, e.g. study_data/participants; "
            "legacy JSON paths import once"
        ),
    )
    serve.add_argument("--display-name")
    serve.add_argument("--interface-version")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(handler=serve_command)

    migrate = subparsers.add_parser(
        "migrate-study-state", help="Split a legacy shared study JSON into participant files"
    )
    migrate.add_argument("--source", type=Path, required=True)
    migrate.add_argument("--output-dir", type=Path, default=Path("study_data/participants"))
    migrate.set_defaults(handler=migrate_study_state_command)

    intake = subparsers.add_parser(
        "serve-intake",
        help="Serve only the expert-study background questionnaire while the analysis runs",
    )
    intake.add_argument("--study-state", type=Path, required=True)
    intake.add_argument("--display-name")
    intake.add_argument("--interface-version")
    intake.add_argument("--host")
    intake.add_argument("--port", type=int)
    intake.set_defaults(handler=serve_intake_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))
