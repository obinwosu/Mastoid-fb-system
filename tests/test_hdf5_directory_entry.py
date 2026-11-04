"""Independent batch-directory boundary tests; no training or server startup."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
GENERATED_ID = f"P{98}T{97}"
PADDED_GENERATED_ID = f"P{98:03d}T{97:03d}"
PADDED_SEPARATED_ID = f"P{98:03d}_T{97:03d}"


def entry_module():
    from evidence_system import directory_pipeline
    return directory_pipeline


def chunk(path):
    """Discovery uses suffixes; actual HDF5 validation is a separate stage."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"discovery-only generated fixture")
    return path


def test_discovery_groups_multiple_chunks_per_directory_without_merging_nested_cases(tmp_path):
    parent = tmp_path / "root_recording"
    child = parent / "nested_recording"
    sibling = tmp_path / "another_recording"
    chunk(parent / "chunk_b.HDF5")
    chunk(parent / "chunk_a.h5")
    chunk(child / "chunk_c.H5")
    chunk(sibling / "chunk_d.hdf5")
    (tmp_path / "not_a_file.hdf5").mkdir()
    (sibling / "unrelated.mp4").write_bytes(b"not used for grouping")
    recordings = entry_module().discover_recordings(tmp_path)
    assert set(recordings) == {parent.resolve(), child.resolve(), sibling.resolve()}
    assert len(recordings) == 3
    assert entry_module().discover_recordings(tmp_path) == recordings


def test_input_recording_itself_is_discovered_once_and_no_hdf5_is_rejected(tmp_path):
    recording = tmp_path / "one_recording"
    chunk(recording / "first.hdf5")
    chunk(recording / "second.H5")
    assert entry_module().discover_recordings(recording) == [recording.resolve()]
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises((ValueError, FileNotFoundError), match="HDF5|hdf5|recording"):
        entry_module().discover_recordings(empty)


@pytest.mark.parametrize("name", [PADDED_GENERATED_ID, PADDED_SEPARATED_ID, "generated_anatX_" + PADDED_GENERATED_ID])
def test_historical_suffix_ids_are_normalized_without_leading_zeros(tmp_path, name):
    assert entry_module().run_id_for_directory(tmp_path / name) == GENERATED_ID


def test_nonhistorical_names_are_safe_stable_and_distinct_across_parent_paths(tmp_path):
    left = tmp_path / "left" / "recording name with spaces"
    right = tmp_path / "right" / "recording name with spaces"
    first = entry_module().run_id_for_directory(left)
    assert first == entry_module().run_id_for_directory(left)
    assert first != entry_module().run_id_for_directory(right)
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", first)


def runtime_file(folder):
    (folder / "simulator").mkdir(exist_ok=True)
    (folder / "bundle").mkdir(exist_ok=True)
    (folder / "corrections.csv").write_text("generated test authority placeholder")
    path = folder / "runtime.json"
    path.write_text(json.dumps({
        "schema": "hdf5_directory_runtime_v1",
        "simulator_root": str(folder / "simulator"),
        "correction_table": str(folder / "corrections.csv"),
        "phase_model_bundle": str(folder / "bundle"),
    }))
    return path


def test_default_root_config_resolves_relative_assets_from_installation_not_cwd(tmp_path, monkeypatch):
    project = tmp_path / "relocated app"
    project.mkdir()
    payload = {
        "schema": "hdf5_directory_runtime_v1", "simulator_root": "assets/simulator",
        "edt_root": "separate grids", "correction_table": "data/corrections.csv",
        "phase_model_bundle": ".local/models/frozen",
        "video_fps": 12.0,
    }
    for key in ("simulator_root", "edt_root", "phase_model_bundle"):
        (project / payload[key]).mkdir(parents=True)
    for key in ("correction_table",):
        path = project / payload[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated fixture; never executed")
    root_config = project / "config.local.json"
    root_config.write_text(json.dumps(payload))
    old_default = project / "config/local/hdf5_runtime.local.json"
    old_default.parent.mkdir(parents=True)
    old_default.write_text("This obsolete default must not be read.")
    unrelated = tmp_path / "caller directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    loaded = entry_module().load_directory_runtime(None, project_root=project)
    assert loaded["configuration_path"] == root_config.resolve()
    for key in ("simulator_root", "edt_root", "correction_table", "phase_model_bundle"):
        assert loaded[key] == (project / payload[key]).resolve()
    assert loaded["video_fps"] == 12.0


@pytest.mark.parametrize("interpreter", ["omitted", None, "nonexistent legacy interpreter"])
def test_legacy_phase_python_is_ignored_without_rewriting_configuration(tmp_path, interpreter):
    config_path = runtime_file(tmp_path)
    payload = json.loads(config_path.read_text())
    if interpreter != "omitted":
        payload["phase_python"] = interpreter
    config_path.write_text(json.dumps(payload))
    original = config_path.read_bytes()
    loaded = entry_module().load_directory_runtime(config_path, project_root=tmp_path)
    assert "phase_python" not in loaded
    assert config_path.read_bytes() == original


@pytest.mark.parametrize("edt_value", ["omitted", None])
def test_explicit_legacy_runtime_without_edt_root_keeps_simulator_layout(tmp_path, edt_value):
    original_path = runtime_file(tmp_path)
    payload = json.loads(original_path.read_text())
    if edt_value is None:
        payload["edt_root"] = None
    original_path.write_text(json.dumps(payload))
    original = original_path.read_bytes()
    # Explicit old runtime files remain readable even if the new default is absent
    # or unrelated. This does not install a second default configuration.
    (tmp_path / "config.local.json").write_text("Unrelated new default")
    loaded = entry_module().load_directory_runtime(original_path, project_root=tmp_path)
    assert loaded["configuration_path"] == original_path.resolve()
    assert loaded["edt_root"] == loaded["simulator_root"] == (tmp_path / "simulator").resolve()
    assert original_path.read_bytes() == original


@pytest.mark.parametrize("location", ["existing", "inside_input", "ancestor_of_input"])
def test_output_cannot_overwrite_or_overlap_input(tmp_path, location):
    raw = tmp_path / "raw" / "recording"
    source = chunk(raw / "one.hdf5")
    before = source.read_bytes()
    if location == "existing":
        output = tmp_path / "existing"
        output.mkdir()
        (output / "keep.txt").write_text("preserve")
    elif location == "inside_input":
        output = raw / "outputs"
    else:
        output = tmp_path / "raw"
    with pytest.raises((ValueError, FileExistsError), match="output|Output|exist|input|Input"):
        entry_module().process_hdf5_directory(
            raw, runtime_config=runtime_file(tmp_path), output_dir=output, prepare_only=True,
        )
    assert source.read_bytes() == before
    if location == "existing":
        assert (output / "keep.txt").read_text() == "preserve"
    elif location == "inside_input":
        assert not output.exists()


def test_duplicate_normalized_recording_ids_fail_before_any_output_is_created(tmp_path):
    raw = tmp_path / "raw"
    chunk(raw / ("left_" + PADDED_GENERATED_ID) / "one.hdf5")
    chunk(raw / f"right_P{98}_T{97}" / "two.hdf5")
    output = tmp_path / "new_batch"
    with pytest.raises(ValueError, match="[Dd]uplicate|collision|ambiguous"):
        entry_module().process_hdf5_directory(
            raw, runtime_config=runtime_file(tmp_path), output_dir=output, prepare_only=True,
        )
    assert not output.exists()


@pytest.mark.parametrize("prior_environment", [None, "previous_profile_configuration"])
@pytest.mark.parametrize("with_reference", [False, True])
def test_batch_isolates_preparation_and_build_failures_restores_environment_and_serves_only_successes(
    tmp_path, monkeypatch, prior_environment, with_reference,
):
    entry = entry_module()
    raw = tmp_path / "raw"
    sources = [chunk(raw / name / "one.hdf5") for name in ("a_bad_prepare", "b_bad_build", "c_success")]
    original_bytes = {path: path.read_bytes() for path in sources}
    if prior_environment is None:
        monkeypatch.delenv("MASTOID_ANATOMY_PROFILES", raising=False)
    else:
        monkeypatch.setenv("MASTOID_ANATOMY_PROFILES", prior_environment)
    prepared = []
    built = []

    def prepare(raw, run_id, case_dir, runtime, anatomy_model, project_root):
        prepared.append(raw.name)
        assert os.environ.get("MASTOID_ANATOMY_PROFILES") == prior_environment
        if raw.name == "a_bad_prepare":
            raise ValueError("generated preparation failure")
        profile = case_dir / "config/anatomy_profiles.local.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"fixture": raw.name}))
        return (
            SimpleNamespace(run_id=run_id), SimpleNamespace(raw_run_dir=raw), profile,
            {"fixture": raw.name}, {"alignment_path": str(case_dir / "alignment.csv")},
        )

    def build(**kwargs):
        source = kwargs["sources"].raw_run_dir
        case_dir = kwargs["result_dir"]
        built.append(source.name)
        assert Path(os.environ["MASTOID_ANATOMY_PROFILES"]) == case_dir / "config/anatomy_profiles.local.json"
        assert kwargs["build_video"] is True
        if source.name == "b_bad_build":
            raise RuntimeError("generated build failure")
        (case_dir / "stages/08_under_ledge_ray").mkdir(parents=True)
        return {
            "evidence_database": str(case_dir / "evidence.sqlite3"),
            "review_video": str(case_dir / "review_video.webm"),
        }

    monkeypatch.setattr(entry, "_prepare_case", prepare)
    monkeypatch.setattr("evidence_system.pipeline.build_evidence", build)
    runtime_path = runtime_file(tmp_path)
    reference = generated_motion_reference(tmp_path / "reference.json") if with_reference else None
    if reference:
        settings = json.loads(runtime_path.read_text())
        settings["motion_reference"] = str(reference)
        runtime_path.write_text(json.dumps(settings))
    output = tmp_path / "new_batch"
    summary = entry.process_hdf5_directory(
        raw, runtime_config=runtime_path, output_dir=output, base_port=18810,
    )
    assert prepared == ["a_bad_prepare", "b_bad_build", "c_success"]
    assert built == ["b_bad_build", "c_success"]
    assert os.environ.get("MASTOID_ANATOMY_PROFILES") == prior_environment
    assert summary["status"] == "partial_failure"
    assert summary["failed_count"] == 2 and summary["completed_count"] == 1
    assert [case["status"] for case in summary["cases"]] == ["failed", "failed", "completed"]
    assert summary["cases"][0]["error_type"] == "ValueError"
    assert summary["cases"][1]["error_type"] == "RuntimeError"
    assert json.loads((output / "batch_summary.json").read_text()) == summary
    assert {path: path.read_bytes() for path in sources} == original_bytes

    from evidence_system.cli import _manifest_serve_options, build_parser
    # Never read the developer's optional default during a synthetic boundary test.
    monkeypatch.setattr("evidence_system.cli._configured_motion_reference", lambda: None)
    manifest_path = Path(summary["recommendations_manifest"])
    manifest = json.loads(manifest_path.read_text())
    case, = manifest["cases"]
    assert case["run_id"] == entry.run_id_for_directory(raw / "c_success")
    assert case["port"] == 18812  # Failed cases do not silently retarget assigned URLs.
    assert manifest["data_access"] == {"expert_annotations": "none", "participant_responses": "none"}
    assert "study_state" not in case
    for field in ("evidence_database", "review_video", "review_video_manifest", "ray_v3_directory"):
        assert Path(case[field]).is_absolute()
    options = _manifest_serve_options(build_parser().parse_args([
        "serve", "--manifest", str(manifest_path), "--case-id", case["case_id"],
    ]))
    assert options["ui_dir"] == (ROOT / "apps/recommendations").resolve()
    assert options["run_id"] == case["run_id"] and options["port"] == 18812
    assert options["db_path"] == Path(case["evidence_database"])
    assert options["ray_v3_dir"] == Path(case["ray_v3_directory"])
    assert options["cohort_benchmark_path"] == reference
    if reference:
        assert case["cohort_benchmark"] == str(reference)
        import hashlib
        assert case["cohort_benchmark_sha256"] == hashlib.sha256(reference.read_bytes()).hexdigest()
    else:
        assert "cohort_benchmark" not in case
        assert "cohort_benchmark_sha256" not in case
    assert options["available_runs"] == [{"run_id": case["run_id"], "url": "http://127.0.0.1:18812/"}]


@pytest.mark.parametrize("separate_edt_root", [False, True])
def test_prepare_only_writes_reusable_case_config_without_fitting_or_video(tmp_path, monkeypatch, separate_edt_root):
    entry = entry_module()
    from evidence_system import pipeline  # Load ordinary dependencies before isolated adapters.
    raw = tmp_path / "raw_recording"
    chunk(raw / "one.hdf5")
    runtime_path = runtime_file(tmp_path)
    expected_edt_root = tmp_path / ("detached EDT root" if separate_edt_root else "simulator")
    if separate_edt_root:
        expected_edt_root.mkdir()
        payload = json.loads(runtime_path.read_text())
        payload["edt_root"] = str(expected_edt_root)
        runtime_path.write_text(json.dumps(payload))
    edt = expected_edt_root / "edt_generated"
    edt.mkdir()
    calls = []

    def configure(**kwargs):
        calls.append(("anatomy", kwargs))
        return SimpleNamespace(
            anatomy_key="generated_anatomy", edt_dir=edt,
            profile={"generated_fixture": True},
            metadata={"selection": "explicit_generated", "mode": "new_recording_model_geometry"},
        )

    def clock(source, output, fps):
        calls.append(("clock", {"source": source, "fps": fps}))
        output.mkdir(parents=True)
        path = output / "alignment.csv"
        path.write_text("frame_idx_global,video_time_s\n0,0\n1,1\n")
        return {"alignment_path": str(path.resolve()), "duration_s": 1.0, "frame_count": 2}

    monkeypatch.setitem(sys.modules, "evidence_system.anatomy_catalog", SimpleNamespace(configure_anatomy=configure))
    monkeypatch.setitem(sys.modules, "evidence_system.hdf5_clock", SimpleNamespace(generate_hdf5_alignment=clock))
    def validate(record, sources, *, project_root):
        assert sources.phase_annotation_file is None
        assert sources.phase_model_bundle == tmp_path / "bundle"
        assert Path(os.environ["MASTOID_ANATOMY_PROFILES"]).is_file()
        return SimpleNamespace(manifest={"status": "generated_test_preflight"})

    monkeypatch.setattr("evidence_system.r4_provenance.validate_production_geometry", validate)
    monkeypatch.setattr("evidence_system.source_audit.build_source_audit", lambda *args, **kwargs: {
        "ready_for_frame_extraction": True, "blocking_items": [],
    })
    monkeypatch.setattr(pipeline, "build_evidence", lambda **kwargs: pytest.fail("Prepare-only must not execute a build"))
    before = os.environ.get("MASTOID_ANATOMY_PROFILES")
    summary = entry.process_hdf5_directory(
        raw, anatomy_model="generated_model", runtime_config=runtime_path,
        output_dir=tmp_path / "prepared", prepare_only=True,
    )
    assert summary["status"] == "prepared" and summary["prepared_count"] == 1
    assert summary["completed_count"] == 0 and summary["failed_count"] == 0
    assert "recommendations_manifest" not in summary
    assert os.environ.get("MASTOID_ANATOMY_PROFILES") == before
    assert calls[0][1]["anatomy_model"] == "generated_model"
    assert calls[0][1]["edt_root"] == expected_edt_root.resolve()
    assert calls[0][1]["simulator_root"] == (tmp_path / "simulator").resolve()
    assert calls[1][1] == {"source": raw.resolve(), "fps": 10.0}
    case_dir = Path(summary["cases"][0]["result_directory"])
    from evidence_system.registry import load_registry
    from evidence_system.source_config import load_source_roots, resolve_run_sources
    record, = load_registry(case_dir / "config/runs.local.csv")
    assert record.anatomy_mapping_status == "configured_model_sources_unverified_recording"
    roots = load_source_roots(case_dir / "config/source_paths.local.json")
    resolved = resolve_run_sources(record, roots)
    assert resolved.raw_run_dir == raw.resolve() and resolved.edt_dir == edt.resolve()
    assert resolved.phase_annotation_file is None
    assert resolved.phase_model_bundle == (tmp_path / "bundle").resolve()
    assert resolved.phase_duration_s == 1.0
    assert resolved.frame_alignment_file.is_file()
    assert (case_dir / "config/anatomy_profiles.local.json").is_file()
    assert (case_dir / "config/anatomy_resolution.json").is_file()
    assert (case_dir / "preflight.json").is_file()
    saved_source = case_dir / "config/source_paths.local.json"
    original = saved_source.read_bytes()
    (tmp_path / "config.local.json").write_text("A new runtime default must not alter saved case sources")
    loaded_again = load_source_roots(saved_source, project_root=tmp_path)
    resolved_again = resolve_run_sources(record, loaded_again)
    assert resolved_again == resolved
    assert saved_source.read_bytes() == original


@pytest.mark.parametrize("span_s", [0.0, 0.999, 1.0])
def test_actual_native_clock_short_recording_boundary_never_fabricates_duration(tmp_path, monkeypatch, span_s):
    import h5py
    import numpy as np
    from evidence_system import pipeline  # Resolve ordinary modules before the isolated anatomy adapter.
    entry = entry_module()
    raw = tmp_path / "generated_recording"
    raw.mkdir()
    source = raw / "generated.H5"
    times = [100.0] if span_s == 0 else [100.0, 100.0 + span_s]
    with h5py.File(source, "w") as handle:
        handle.create_dataset("data/time", data=np.asarray(times, dtype=np.float64))
        handle.create_dataset("data/l_img", data=np.zeros((len(times), 4, 4, 3), dtype=np.uint8))
    original = source.read_bytes()
    anatomy = SimpleNamespace(
        anatomy_key="generated_model", edt_dir=tmp_path / "generated_edt", profile={},
        metadata={"mode": "new_recording_model_geometry"},
    )
    monkeypatch.setitem(sys.modules, "evidence_system.anatomy_catalog", SimpleNamespace(
        configure_anatomy=lambda **kwargs: anatomy,
    ))
    monkeypatch.setattr(pipeline, "build_evidence", lambda **kwargs: pytest.fail("Boundary test must not execute inference"))
    runtime = {
        "simulator_root": tmp_path, "correction_table": tmp_path / "generated.csv",
        "video_fps": 10.0, "phase_model_bundle": tmp_path / "frozen_bundle",
    }
    case_dir = tmp_path / "case_output"
    if span_s < 1:
        with pytest.raises(ValueError, match="at least one second.*no duration is fabricated"):
            entry._prepare_case(raw, "generated_case", case_dir, runtime, "generated_model", ROOT)
        assert not (case_dir / "config/source_paths.local.json").exists()
    else:
        record, sources, _, _, clock = entry._prepare_case(
            raw, "generated_case", case_dir, runtime, "generated_model", ROOT,
        )
        assert sources.phase_duration_s == 1.0 and clock["duration_s"] == 1.0
        assert record.anatomy_mapping_status == "configured_model_sources_unverified_recording"
        generated_sources = json.loads((case_dir / "config/source_paths.local.json").read_text())
        assert "phase_python" not in generated_sources
        assert not hasattr(sources, "phase_python")
    receipt = json.loads((case_dir / "clock/hdf5_clock_manifest.json").read_text())
    assert receipt["duration_s"] == times[-1] - times[0]
    assert receipt["raw_time_bounds_abs"] == [times[0], times[-1]]
    assert source.read_bytes() == original


def test_cli_rejects_prepare_plus_serve_before_any_work(monkeypatch, tmp_path):
    from evidence_system import cli
    monkeypatch.setattr(entry_module(), "process_hdf5_directory", lambda *args, **kwargs: pytest.fail("Must reject before preparation"))
    with pytest.raises(ValueError, match="prepare-only"):
        cli.main(["process-hdf5", "--input-dir", str(tmp_path), "--prepare-only", "--serve"])


@pytest.mark.parametrize("failed_count,serve_result,expected_failure", [
    (0, 0, False), (1, 0, True), (0, 3, True), (1, 3, True),
])
def test_cli_reports_build_or_server_failure_in_exit_status(tmp_path, monkeypatch, failed_count, serve_result, expected_failure):
    from evidence_system import cli
    manifest = tmp_path / "generated manifest.json"
    monkeypatch.setattr(entry_module(), "process_hdf5_directory", lambda *args, **kwargs: {
        "failed_count": failed_count, "recommendations_manifest": str(manifest),
        "status": "partial_failure" if failed_count else "completed", "output_directory": str(tmp_path),
    })
    served = []
    def serve(args):
        served.append(args.manifest)
        return serve_result
    monkeypatch.setattr(cli, "serve_batch_command", serve)
    result = cli.main(["process-hdf5", "--input-dir", str(tmp_path), "--serve"])
    assert (result != 0) is expected_failure
    assert served == [manifest]


def serve_manifest(path, *, ports=(18810, 18811), identifiers=("generated_a", "generated_b")):
    path.write_text(json.dumps({
        "app_mode": "recommendations", "host": "127.0.0.1",
        "data_access": {"expert_annotations": "none", "participant_responses": "none"},
        "cases": [{"case_id": identifier, "run_id": identifier, "port": port}
                  for identifier, port in zip(identifiers, ports, strict=True)],
    }))
    return path


@pytest.mark.parametrize("ports,identifiers", [
    ((18810, 18810), ("generated_a", "generated_b")),
    ((18810, 65536), ("generated_a", "generated_b")),
    ((18810, 18811), ("same_case", "same_case")),
])
def test_batch_serve_rejects_port_or_identity_collisions_without_launching(tmp_path, monkeypatch, ports, identifiers):
    from evidence_system import cli
    import socket
    import subprocess
    path = serve_manifest(tmp_path / "manifest.json", ports=ports, identifiers=identifiers)
    monkeypatch.setattr(socket, "socket", lambda: pytest.fail("Invalid manifest must not probe ports"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Invalid manifest must not launch"))
    with pytest.raises(ValueError, match="unique|ports"):
        cli.main(["serve-batch", "--manifest", str(path)])


def test_batch_serve_occupied_port_cannot_start_or_stop_other_services(tmp_path, monkeypatch):
    from evidence_system import cli
    import socket
    import subprocess
    path = serve_manifest(tmp_path / "manifest.json")
    class OccupiedPort:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def bind(self, address):
            raise OSError("generated occupied port")
    monkeypatch.setattr(socket, "socket", OccupiedPort)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Occupied port must prevent all launches"))
    with pytest.raises(OSError, match="occupied"):
        cli.main(["serve-batch", "--manifest", str(path)])


@pytest.mark.parametrize("failure", ["second_launch", "child_exit", "interrupt"])
def test_batch_launcher_preserves_argv_and_cleans_up_only_its_children(tmp_path, monkeypatch, failure):
    from evidence_system import cli
    import socket
    import subprocess
    import time
    path = serve_manifest(tmp_path / "manifest with spaces.json")
    class AvailablePort:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def bind(self, address):
            assert address[0] == "127.0.0.1"
    class Child:
        def __init__(self, code=None):
            self.returncode = code
            self.terminated = False
            self.waited = False
        def poll(self):
            return self.returncode
        def terminate(self):
            self.terminated = True
            self.returncode = -15
        def wait(self, timeout=None):
            self.waited = True
            return self.returncode
    launched = []
    def launch(argv, **kwargs):
        if failure == "second_launch" and launched:
            raise OSError("generated second child failure")
        assert argv[:2] == [sys.executable, str(ROOT / "run.py")]
        assert argv[2:5] == ["serve", "--manifest", str(path)]
        assert argv[5:] == ["--case-id", "generated_a" if not launched else "generated_b"]
        assert kwargs["cwd"] == str(ROOT)
        child = Child(4 if failure == "child_exit" and not launched else None)
        launched.append(child)
        return child
    def interrupt(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(socket, "socket", AvailablePort)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(time, "sleep", interrupt)
    if failure == "second_launch":
        with pytest.raises(OSError, match="second child"):
            cli.main(["serve-batch", "--manifest", str(path)])
    else:
        code = cli.main(["serve-batch", "--manifest", str(path)])
        assert code == (4 if failure == "child_exit" else 0)
    assert launched and all(child.waited for child in launched)
    assert all(child.terminated for child in launched if child.returncode != 4)


def generated_motion_reference(path):
    """Real schema/functions, entirely generated rows and declared fixture provenance."""
    from evidence_system.motion_comparison import (
        build_motion_windows, build_expert_reference, write_motion_artifact,
    )
    rows = [{
        "t_abs": 100 + i / 4, "t_video_s": i / 4,
        "drill_x": i / 4000, "drill_y": 0.0, "drill_z": 0.0,
        "frame_removed_mm3": 0.25, "phase": "early_surface", "phase_id": "generated_phase",
    } for i in range(25)]
    build = build_motion_windows(
        rows, global_run_id="generated_reference_run", participant_id="generated_expert",
        provenance={
            "pose_source": "canonical_vision_pose", "position_unit": "m",
            "phase_source_kind": "human_phase_annotation", "clock_source": "generated_native_clock",
            "source_artifacts": [
                {"kind": kind, "path": f"generated_fixture/{kind}", "sha256": "a" * 64}
                for kind in ("frames", "phases")
            ],
        },
    )
    reference = build_expert_reference(
        [build], expert_participant_ids=["generated_expert"],
        provenance={"expert_definition": "Generated unit-test identity; no real expertise claim"},
    )
    write_motion_artifact(path, reference)
    return path.resolve()


@pytest.mark.parametrize("value", ["omitted", None])
def test_motion_reference_setting_is_optional_without_rewriting_old_runtime(tmp_path, value):
    path = runtime_file(tmp_path)
    payload = json.loads(path.read_text())
    if value is None:
        payload["motion_reference"] = None
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    result = entry_module().load_directory_runtime(path, project_root=tmp_path)
    assert "motion_reference" not in result
    assert path.read_bytes() == before


def test_motion_reference_runtime_resolves_relative_path_and_validates_content(tmp_path):
    path = runtime_file(tmp_path)
    reference = generated_motion_reference(tmp_path / "reference.json")
    payload = json.loads(path.read_text())
    payload["motion_reference"] = "reference.json"
    path.write_text(json.dumps(payload))
    original = reference.read_bytes()
    loaded = entry_module().load_directory_runtime(path, project_root=tmp_path)
    assert loaded["motion_reference"] == reference
    assert reference.read_bytes() == original
    tampered = json.loads(original)
    tampered["recordings"][0]["windows"][0]["speed_mm_s"] += 1
    reference.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="digest|schema"):
        entry_module().load_directory_runtime(path, project_root=tmp_path)
    reference.unlink()
    with pytest.raises(FileNotFoundError, match="motion_reference"):
        entry_module().load_directory_runtime(path, project_root=tmp_path)


def test_default_motion_reference_uses_only_the_temporary_installation_config(tmp_path, monkeypatch):
    from evidence_system import cli
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli._configured_motion_reference() is None
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"motion_reference": None}))
    assert cli._configured_motion_reference() is None
    reference = generated_motion_reference(tmp_path / "reference.json")
    config.write_text(json.dumps({"motion_reference": str(reference)}))
    assert cli._configured_motion_reference() == reference
    reference.unlink()
    with pytest.raises(FileNotFoundError, match="motion_reference"):
        cli._configured_motion_reference()


@pytest.mark.parametrize("mode", ["explicit", "default", "missing_explicit", "tampered_explicit"])
def test_manifest_reference_binding_never_silently_replaces_an_explicit_reference(tmp_path, monkeypatch, mode):
    import hashlib
    from evidence_system import cli
    fallback = generated_motion_reference(tmp_path / "default_reference.json")
    explicit = generated_motion_reference(tmp_path / "case_reference.json")
    monkeypatch.setattr(cli, "_configured_motion_reference", lambda: fallback)
    case = {
        "case_id": "generated_case", "run_id": "generated_case", "port": 18810,
        "evidence_database": str(tmp_path / "evidence.sqlite3"),
        "review_video": str(tmp_path / "review.webm"),
        "review_video_manifest": str(tmp_path / "video_manifest.json"),
    }
    if mode != "default":
        case["cohort_benchmark"] = str(explicit)
        case["cohort_benchmark_sha256"] = hashlib.sha256(explicit.read_bytes()).hexdigest()
    if mode == "missing_explicit":
        explicit.unlink()
    elif mode == "tampered_explicit":
        explicit.write_bytes(explicit.read_bytes() + b"\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "app_mode": "recommendations", "ui_directory": str(ROOT / "apps/recommendations"),
        "data_access": {"expert_annotations": "none", "participant_responses": "none"},
        "cases": [case],
    }))
    args = cli.build_parser().parse_args(["serve", "--manifest", str(manifest)])
    if mode == "missing_explicit":
        with pytest.raises(FileNotFoundError, match="reference|benchmark|cohort"):
            cli._manifest_serve_options(args)
    elif mode == "tampered_explicit":
        with pytest.raises(ValueError, match="reference.*manifest"):
            cli._manifest_serve_options(args)
    else:
        options = cli._manifest_serve_options(args)
        assert options["cohort_benchmark_path"] == (explicit if mode == "explicit" else fallback)


def generated_r4_motion_catalog(tmp_path, monkeypatch, *, defect=None):
    """Mock only the trusted manifest pin for a self-contained generated catalog."""
    import csv
    import hashlib
    from evidence_system import cli
    catalog = tmp_path / "generated_catalog"
    hashes = []

    def write_csv(relative, rows):
        target = catalog / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        hashes.append({"Path": relative, "SHA256": hashlib.sha256(target.read_bytes()).hexdigest()})
        return target

    write_csv("participants.csv", [
        {"ParticipantID": "generated_expert", "TrainingYears": 11},
        {"ParticipantID": "generated_partial_expert", "TrainingYears": 12},
        {"ParticipantID": "generated_novice", "TrainingYears": 10},
    ])
    records = [{
        "RecordingID": rid, "ParticipantID": person, "PhaseStatus": status,
        "RawFolder": folder, "FrameRows": 101, "ActualSpecimen": "generated_anatomy",
        "AssistanceCondition": "generated_condition",
    } for rid, person, status, folder in [
        ("generated_complete", "generated_expert", "complete", "frames/generated_complete"),
        ("generated_partial", "generated_partial_expert", "partial", "intentionally_absent"),
        ("generated_novice", "generated_novice", "complete", "also_intentionally_absent"),
    ]]
    if defect == "inventory":
        records[0]["FrameRows"] = 102
    write_csv("recordings.csv", records)
    frames, alignment = [], []
    for index in range(101):
        seconds = index / 4
        frames.append({
            "frame_idx_global": index, "t_abs": 100 + seconds,
            "drill_x": seconds / 1000, "drill_y": 0, "drill_z": 0,
            "frame_removed_voxels": 1 if seconds >= 2 else 0,
            "frame_removed_mm3": .25 if seconds >= 2 else 0,
            "is_drilling_frame": seconds >= 2,
            "stroke_id": 0 if seconds >= 2 else "",
            "stroke_end_flag": index == 100,
            "phase": "unused_frame_label",
            "feedback_text": "FORBIDDEN_GENERATED_FEEDBACK_NOT_AN_INPUT",
        })
        alignment.append({
            "frame_idx_global": index,
            # Deliberately different video clock: it provides labels, not window duration.
            "video_time_s": 1000 + 2 * seconds,
            "phase": "pre_antrum" if seconds < 8 else "antrum" if seconds < 14
                     else "incus" if seconds < 19 else "post_incus",
        })
    if defect == "identity":
        alignment[50]["frame_idx_global"] = 51
    if defect == "unknown_phase":
        alignment[50]["phase"] = "unrecognized_phase"
    frame_path = write_csv("frames/generated_complete/frame_level_spatial_all_frames_visibility.csv", frames)
    write_csv("frames/generated_complete/alignment.csv", alignment)
    manifest = catalog / "releases/R4_20261029/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "Files": hashes, "GeometryNeedsReviewRecordingIDs": ["generated_complete"],
        "GeometryScientificAcceptanceStatus": "generated_false_review_fixture",
    }))
    # This override is scoped to generated unit-test bytes, never to the real release.
    monkeypatch.setattr(cli, "TRUSTED_R4_CATALOG_SHA256", hashlib.sha256(manifest.read_bytes()).hexdigest())
    if defect == "source_hash":
        with frame_path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
    return catalog


def test_cli_builds_native_time_expert_windows_from_verified_manual_phase_rows(tmp_path, monkeypatch):
    import hashlib
    from evidence_system import cli
    from evidence_system.motion_comparison import validate_expert_reference
    catalog = generated_r4_motion_catalog(tmp_path, monkeypatch)
    output = tmp_path / "generated_reference.json"
    summary = cli.build_motion_reference(catalog, output)
    payload = json.loads(output.read_text())
    validate_expert_reference(payload)
    assert summary["recording_count"] == 1
    assert summary["excluded_recordings"] == [{
        "recording_id": "generated_partial", "reason": "incomplete_manual_phase_boundaries",
    }]
    assert summary["reference_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert payload["expert_participant_ids"] == ["generated_expert", "generated_partial_expert"]
    record, = payload["recordings"]
    assert record["stroke_frequency_metadata"]["source_status"] == "recorded_legacy_segmentation"
    assert all("stroke_frequency_hz" in row and "stroke_rate_per_s" in row for row in record["windows"])
    assert record["native_time_origin_abs"] == 100 and record["native_duration_s"] == 25
    assert record["source_frame_count"] == 101
    assert record["provenance"]["phase_source_kind"] == "human_phase_annotation"
    assert record["provenance"]["geometry_needs_review"] is True
    assert payload["provenance"]["user_review_performed"] is False
    assert payload["provenance"]["clinician_review_performed"] is False
    assert "FORBIDDEN_GENERATED_FEEDBACK_NOT_AN_INPUT" not in output.read_text()
    windows = record["windows"]
    assert min(row["native_start_s"] for row in windows) == 2  # First removal, not prehistory.
    assert {row["phase"] for row in windows} == {"early_phase", "antrum", "incus"}
    assert len({row["phase_id"] for row in windows if row["phase"] == "incus"}) == 1
    assert [(row["native_start_s"], row["native_end_s"]) for row in windows if row["phase"] == "incus"] == [(14, 19), (19, 24)]
    assert payload["protocol"]["stride_s"] == 5 and payload["protocol"]["persistence_windows"] == 1
    for row in windows:
        assert row["speed_mm_s"] == pytest.approx(1.0)
        assert row["t_start_s"] == row["native_start_s"]
        assert row["t_end_s"] - row["t_start_s"] == 5.0
        assert row["coverage_fraction"] == pytest.approx(1.0)
    before = output.read_bytes()
    with pytest.raises(FileExistsError, match="preserved|existing"):
        cli.build_motion_reference(catalog, output)
    assert output.read_bytes() == before
    with pytest.raises(ValueError, match="outside"):
        cli.build_motion_reference(catalog, catalog / "forbidden_output.json")
    assert not (catalog / "forbidden_output.json").exists()


@pytest.mark.parametrize("defect,match", [
    ("source_hash", "checksum"), ("identity", "identity"),
    ("unknown_phase", "Unknown"), ("inventory", "inventory"),
])
def test_cli_motion_reference_rejects_bad_source_before_writing_output(tmp_path, monkeypatch, defect, match):
    from evidence_system import cli
    catalog = generated_r4_motion_catalog(tmp_path, monkeypatch, defect=defect)
    output = tmp_path / "rejected.json"
    with pytest.raises(ValueError, match=match):
        cli.build_motion_reference(catalog, output)
    assert not output.exists()


def test_cli_motion_reference_requires_the_pinned_release_manifest(tmp_path, monkeypatch):
    from evidence_system import cli
    catalog = generated_r4_motion_catalog(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "TRUSTED_R4_CATALOG_SHA256", "0" * 64)
    output = tmp_path / "rejected.json"
    with pytest.raises(ValueError, match="verified published release"):
        cli.build_motion_reference(catalog, output)
    assert not output.exists()
