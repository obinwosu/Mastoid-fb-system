"""Native image membership, source binding, and current-record motion regression."""

from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import struct
from urllib.parse import parse_qs, urlparse
import zlib

import h5py
import numpy as np
import pytest

from test_ray_feedback_runtime import make_service, write_ray
from evidence_system.hdf5_clock import generate_hdf5_alignment
from evidence_system.frame_pipeline.core_frames import resolve_frame_clock
from evidence_system.motion_comparison import LEGACY_PROTOCOL, build_motion_windows, build_expert_reference, artifact_sha256
from evidence_system.web import _handler


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def bound_case(tmp_path, *, schema="legacy", seconds=8):
    root = tmp_path / "run"
    service = make_service(root)
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "chunk.hdf5"
    times = 100 + np.arange(int(seconds * 10) + 1) / 10
    with h5py.File(source, "w") as handle:
        prefix = "data/" if schema == "legacy" else "vision_data/continuous_data/"
        handle.create_dataset(prefix + "time", data=times)
        images = np.zeros((len(times), 2, 2, 3), dtype=np.uint8)
        images[:] = [7, 41, 223]  # Native BGR: browser RGB must be 223,41,7.
        handle.create_dataset(prefix + "l_img", data=images)
        if schema == "v3":
            prefix = "physics_data/continuous_data/high_frequency_poses/main_camera_pose/"
            handle.create_dataset(prefix + "time_stamp", data=times)
            handle.create_dataset(prefix + "pose", data=np.tile([0, 0, 0, 0, 0, 0, 1], (len(times), 1)))
    generated = generate_hdf5_alignment(raw, root / "clock")
    parts = [{"t_abs": float(t), "source_hdf5": source.name, "source_frame_idx": i}
             for i, t in enumerate(times)]
    aligned, clock = resolve_frame_clock(parts, Path(generated["alignment_path"]),
                                        global_run_id="synthetic:case", raw_run_dir=raw)
    stages = root / "stages"
    stroke = stages / "05_strokes/strokes.csv"
    stroke.parent.mkdir(parents=True)
    stroke.write_text("stroke_id\n")
    phase = stages / "03_phase_detection/manifest.json"
    dump(phase, {"global_run_id": "synthetic:case", "source_kind": "model_phase_prediction",
                 "clock": clock, "checkpoint_sha256": "a" * 64})
    core = stages / "01_core_frames/extraction_manifest.json"
    dump(core, {"global_run_id": "synthetic:case", "clock_alignment": clock})
    audit = {"global_run_id": "synthetic:case", "source_classification": {"primary_raw": str(raw)},
             "hdf5": {"chunks": [{"file_name": source.name, "path": str(source), "sha256": sha(source)}]}}
    dump(stages / "00_source_audit/source_manifest.json", audit)
    audit_hash = hashlib.sha256(json.dumps(audit, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
    frames = []
    with sqlite3.connect(service.retriever.db_path) as connection:
        connection.execute("UPDATE runs SET source_manifest_hash=?", (audit_hash,))
        connection.execute("INSERT INTO generated_artifacts VALUES (?,?,?,?,?,?,?,?,?)",
            ("stroke", "synthetic:case", "stroke_build", str(stroke), sha(stroke), "test", "test", "test", "test"))
        for name, kind, path in [("raw", "raw_hdf5", source), ("phase", "model_phase_prediction", phase)]:
            connection.execute("INSERT INTO source_files VALUES (?,?,?,?,?,?,?,?)",
                (name, "synthetic:case", kind, str(path), sha(path), path.stat().st_size, "test", 1))
        connection.execute("INSERT INTO phases VALUES (?,?,?,?,?,?,?)",
            ("phase:antrum", "synthetic:case", "antrum", "antrum", 0, seconds, "phase"))
        for index, item in enumerate(aligned):
            row = {**parts[index], "frame_idx_global": index, "t_video_s": item["video_time_s"],
                   "phase_id": "phase:antrum", "phase": "antrum", "model_phase": "antrum",
                   "drill_x": index * .0001, "drill_y": 0, "drill_z": 0, "frame_removed_mm3": .1}
            frames.append(row)
            connection.execute("INSERT INTO frames VALUES (?,?,?,?,?,?,?,?)",
                (f"frame:{index}", "synthetic:case", index, row["t_video_s"], "phase:antrum", "raw", index, json.dumps(row)))
    write_ray(stages / "08_under_ledge_ray")
    scores = stages / "08_under_ledge_ray/ray_v3_frame_scores.csv"
    scores.write_text("frame_idx_global,t_video_s,blocked_contact_fraction,registered_contact_voxel_count\n"
                      "15,1.5,0.85,30\n")
    episodes = stages / "08_under_ledge_ray/ray_v3_candidate_episodes.json"
    values = json.loads(episodes.read_text())
    values[0]["source_frame_indices"] = [15]
    dump(episodes, values)
    return service, frames, source, stages


def first_clip(service):
    return service.bootstrap()["coaching_plan"]["items"][0]["review_clips"][0]


@pytest.mark.parametrize("schema", ["legacy", "v3"])
def test_native_exact_keyframe_and_browser_png_color(tmp_path, schema):
    service, _, _, _ = bound_case(tmp_path, schema=schema)
    clip = first_clip(service)
    assert clip["keyframe_available"] and clip["keyframe_time_s"] == 1.5
    assert clip["keyframe_frame_idx_global"] == 15
    token = parse_qs(urlparse(clip["keyframe_url"]).query)["clip_id"][0]
    png = service.original_review_frame(token)
    # Check actual PNG bytes, not an OpenCV round trip that could hide channel swapping.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    offset, compressed = 8, b""
    while offset < len(png):
        size = struct.unpack(">I", png[offset:offset+4])[0]
        kind, data = png[offset+4:offset+8], png[offset+8:offset+8+size]
        if kind == b"IHDR":
            assert data[8:10] == bytes([8, 2])  # Eight-bit truecolor RGB.
        if kind == b"IDAT":
            compressed += data
        offset += 12 + size
    scanlines = zlib.decompress(compressed)
    # First pixel of the first row is unpredicted for PNG filter 0,1,2,3,4.
    assert scanlines[1:4] == bytes([223, 41, 7])
    assert service.original_review_frame(token) == png


@pytest.mark.parametrize("damage", ["source", "clock", "sidecar", "wrong_path", "missing_index", "anchor_gap"])
def test_unbound_or_missing_anchor_has_no_nearest_or_video_fallback(tmp_path, damage):
    service, _, source, stages = bound_case(tmp_path)
    clip = first_clip(service)
    token = parse_qs(urlparse(clip["keyframe_url"]).query)["clip_id"][0]
    if damage == "source":
        with h5py.File(source, "r+") as handle:
            handle["data/l_img"][15] = 0
    elif damage == "clock":
        (service.artifact_root / "clock/alignment.csv").write_text("changed")
    elif damage == "sidecar":
        (service.artifact_root / "clock/hdf5_clock_manifest.json").unlink()
    elif damage in {"wrong_path", "missing_index"}:
        with sqlite3.connect(service.retriever.db_path) as connection:
            if damage == "wrong_path":
                connection.execute("UPDATE source_files SET path=? WHERE source_file_id='raw'", (str(tmp_path / "other.hdf5"),))
            else:
                connection.execute("DELETE FROM frames WHERE frame_idx=15")
    else:
        (stages / "08_under_ledge_ray/ray_v3_frame_scores.csv").write_text(
            "frame_idx_global,t_video_s,blocked_contact_fraction,registered_contact_voxel_count\n15,1.55,0.85,30\n")
    assert first_clip(service)["keyframe_available"] is False
    with pytest.raises(ValueError):
        service.original_review_frame(token)


def test_old_archive_coaching_remains_without_fabricated_native_image(tmp_path):
    service = make_service(tmp_path / "archive")
    write_ray(service.artifact_root / "stages/08_under_ledge_ray")
    clip = first_clip(service)
    assert clip["anchor_t_s"] == 1.5 and not clip["keyframe_available"]
    assert "keyframe_url" not in clip


def test_endpoint_cannot_accept_file_or_arbitrary_index(tmp_path):
    service, _, _, _ = bound_case(tmp_path)
    clip = first_clip(service)
    handler = object.__new__(_handler(service, tmp_path))
    handler.wfile = BytesIO()
    statuses, headers = [], {}
    handler.send_response = statuses.append
    handler.send_header = lambda key, value: headers.update({key: value})
    handler.end_headers = lambda: None
    handler._keyframe(urlparse(clip["keyframe_url"] + "&path=elsewhere&frame_idx=2"))
    assert statuses[-1] == 404
    handler.wfile = BytesIO()
    handler._keyframe(urlparse(clip["keyframe_url"]))
    assert statuses[-1] == 200 and headers["Content-Type"] == "image/png"
    assert handler.wfile.getvalue().startswith(b"\x89PNG")
    with pytest.raises(ValueError):
        service.original_review_frame("f" * 64)


def reference_for(service, frames, *, protocol=LEGACY_PROTOCOL):
    people = ["expert_alpha", "expert_beta", "expert_gamma"]
    provenance = {"pose_source": "canonical_vision_pose", "position_unit": "m",
                  "phase_source_kind": "manual", "clock_source": "native_relative_time",
                  "source_artifacts": [{"kind": kind, "path": "synthetic_"+kind, "sha256": "b"*64}
                                       for kind in ("frames", "phases")]}
    builds = []
    for person in people:
        rows = [{**row, "drill_x": row["drill_x"] * 4} for row in frames]
        builds.append(build_motion_windows(rows, global_run_id="reference:"+person,
                      participant_id=person, provenance=provenance, protocol=protocol))
    reference = build_expert_reference(builds, expert_participant_ids=people,
                                      provenance={"expert_definition": "Synthetic test roster"})
    service.cohort_benchmark_path = service.artifact_root / "reference.json"
    dump(service.cohort_benchmark_path, reference)
    return reference


def test_archive_motion_uses_actual_database_not_reference_observed_values(tmp_path):
    service, frames, _, _ = bound_case(tmp_path)
    reference_for(service, frames)
    payload = service.bootstrap()
    comparison = payload["motion_comparison"]
    assert comparison["available"]
    assert comparison["current_source"] == "current_database_observations_archive_fallback"
    assert comparison["windows"][0]["speed_mm_s"] == pytest.approx(1)
    assert comparison["episodes"]
    assert any(item["feedback_id"].startswith("coaching:motion") for item in payload["coaching_plan"]["items"])
    with sqlite3.connect(service.retriever.db_path) as connection:
        for index, row in enumerate(frames):
            row["drill_x"] *= 10
            connection.execute("UPDATE frames SET observation_json=? WHERE frame_idx=?", (json.dumps(row), index))
    current = service.bootstrap()["motion_comparison"]
    assert current["windows"][0]["speed_mm_s"] == pytest.approx(10)
    assert not current["episodes"]


@pytest.mark.parametrize("participant", ["synthetic_performer", ""])
def test_saved_motion_must_match_current_db_and_artifact_binding(tmp_path, participant):
    service, frames, _, stages = bound_case(tmp_path)
    with sqlite3.connect(service.retriever.db_path) as connection:
        connection.execute("UPDATE runs SET participant_id=?", (participant,))
    reference_for(service, frames)
    context = service._bound_review_context(service.retriever.get_run_summary("case")["run"])
    files = {"frames": stages / "05_strokes/frame_observations_complete.csv",
             "phases": stages / "01_core_frames/phase_intervals.csv", "clock": context["core_path"]}
    files["frames"].write_text("synthetic frame artifact")
    files["phases"].write_text("synthetic phase artifact")
    provenance = {"pose_source": "canonical_vision_pose", "position_unit": "m",
                  "phase_source_kind": "model_phase_prediction", "phase_model_sha256": "a"*64,
                  "clock_source": context["clock"]["source"],
                  "source_artifacts": [{"kind": kind, "path": str(path), "sha256": sha(path)} for kind,path in files.items()]}
    saved = build_motion_windows(frames, global_run_id="synthetic:case", participant_id=participant or None,
                                 provenance=provenance, protocol=LEGACY_PROTOCOL)
    sidecar = stages / "09_motion_comparison/motion_windows.json"
    dump(sidecar, saved)
    assert service.bootstrap()["motion_comparison"]["current_source"].startswith("bound_pipeline_sidecar")
    forged = deepcopy(saved)
    forged["windows"][0]["speed_mm_s"] = 99
    forged["artifact_sha256"] = artifact_sha256(forged)
    dump(sidecar, forged)
    assert service.bootstrap()["motion_comparison"]["available"] is False
    dump(sidecar, saved)
    files["frames"].write_text("changed")
    assert service.bootstrap()["motion_comparison"]["available"] is False


@pytest.mark.parametrize("damage", ["manual_archive", "bad_reference"])
def test_unavailable_motion_keeps_existing_nonmotion_feedback(tmp_path, damage):
    service, frames, _, _ = bound_case(tmp_path)
    reference = reference_for(service, frames)
    if damage == "manual_archive":
        with sqlite3.connect(service.retriever.db_path) as connection:
            connection.execute("UPDATE source_files SET source_type='human_phase_annotation' WHERE source_file_id='phase'")
    else:
        reference["recordings"][0]["windows"][0]["speed_mm_s"] = 100
        dump(service.cohort_benchmark_path, reference)
    payload = service.bootstrap()
    assert not payload["motion_comparison"]["available"]
    assert payload["motion_comparison"]["artifact_sha256"] == artifact_sha256(payload["motion_comparison"])
    assert [item["feedback_id"] for item in payload["coaching_plan"]["items"]] == ["coaching:working_corridor"]
