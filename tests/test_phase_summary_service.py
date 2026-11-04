"""Whole-phase service integration uses bound observations and portable references."""

import json
import sqlite3
from io import BytesIO
from pathlib import Path

import h5py
import pytest
from test_ray_feedback_runtime import ROOT, make_service
from test_review_frames import bound_case, dump

from evidence_system.phase_summary import build_phase_expert_reference, build_phase_motion
from evidence_system.web import _handler


def selected(payload, phase="antrum"):
    row = next(row for row in payload["phases"] if row["phase_name"] == phase)
    return row, {metric["key"]: metric for metric in row["metrics"]}


def save_frames(service, rows):
    with sqlite3.connect(service.retriever.db_path) as connection:
        for row in rows:
            connection.execute(
                "UPDATE frames SET observation_json=? WHERE frame_idx=?",
                (json.dumps(row), row["frame_idx_global"]),
            )


def saved_reference(service, frames):
    people = ["expert_a", "expert_b", "expert_c"]
    provenance = {
        "pose_source": "canonical_vision_pose", "position_unit": "m",
        "phase_source_kind": "human_phase_annotation", "clock_source": "native_relative_time",
        "source_artifacts": [
            {"kind": kind, "path": "expert_source_" + kind + ".csv", "sha256": "b" * 64}
            for kind in ("frames", "phases")
        ],
    }
    builds = [
        build_phase_motion(
            [{**row, "drill_x": row["drill_x"] * 4} for row in frames],
            global_run_id="expert:" + person, participant_id=person, provenance=provenance,
        )
        for person in people
    ]
    reference = build_phase_expert_reference(
        builds, expert_participant_ids=people,
        provenance={"expert_definition": "Synthetic test reference"},
    )
    service.phase_reference_path = service.artifact_root / "phase_reference.json"
    dump(service.phase_reference_path, reference)
    return reference


def request(service, route):
    directory = "recommendations" if service.mode == "recommendations" else "expert_study"
    handler = object.__new__(_handler(service, ROOT / "apps" / directory))
    handler.path, handler.headers, handler.wfile = route, {}, BytesIO()
    statuses, headers = [], {}
    handler.send_response = statuses.append
    handler.send_header = lambda key, value: headers.update({key: value})
    handler.end_headers = lambda: None
    handler.do_GET()
    return statuses[-1], headers, handler.wfile.getvalue()


def test_whole_phase_uses_tail_without_reference_or_image_decoding(tmp_path, monkeypatch):
    service, rows, _, _ = bound_case(tmp_path, seconds=8)
    for row in rows:
        index = row["frame_idx_global"]
        if index > 50:
            row["drill_x"] = 0.005 + (index - 50) * 0.0007
            row["frame_removed_mm3"] = 0.3
    save_frames(service, rows)

    def forbidden(*args, **kwargs):
        pytest.fail("Summary attempted to decode or reopen source images")

    monkeypatch.setattr(h5py, "File", forbidden)
    monkeypatch.setattr(service, "_decode_review_frame", forbidden)
    phase, metrics = selected(service.phase_summary())
    assert phase["duration_s"] == pytest.approx(8)
    # First5s:1mm/s and1mm3/s; final3s:7mm/s and3mm3/s.
    assert metrics["tool_speed_mm_s"]["value"] == pytest.approx(26 / 8)
    assert metrics["removal_rate_mm3_s"]["value"] == pytest.approx(14 / 8)
    assert metrics["tool_speed_mm_s"]["available"]
    assert not metrics["tool_speed_mm_s"]["reference"]["available"]
    assert metrics["stroke_length_mm"]["value"] is None
    assert not phase["safety"]["critical_structure_violation"]["available"]
    assert phase["safety"]["critical_structure_violation"]["count"] is None


def test_phase_shorter_than_five_seconds_still_has_current_measurements(tmp_path):
    service, _, _, _ = bound_case(tmp_path, seconds=3.2)
    phase, metrics = selected(service.phase_summary())
    assert phase["duration_s"] == pytest.approx(3.2)
    assert metrics["tool_speed_mm_s"]["available"]
    assert metrics["tool_speed_mm_s"]["value"] == pytest.approx(1)
    assert metrics["removal_rate_mm3_s"]["value"] == pytest.approx(1)
    assert selected(service.phase_summary(), "incus")[1]["tool_speed_mm_s"]["value"] is None


@pytest.mark.parametrize("damage", ["phase", "clock", "frame_identity"])
def test_unbound_current_evidence_does_not_become_zero(tmp_path, damage):
    service, _, _, stages = bound_case(tmp_path)
    if damage == "phase":
        (stages / "03_phase_detection/manifest.json").write_text("{}", encoding="utf-8")
    elif damage == "clock":
        (service.artifact_root / "clock/alignment.csv").write_text("changed", encoding="utf-8")
    else:
        with sqlite3.connect(service.retriever.db_path) as connection:
            connection.execute("UPDATE frames SET t_video_s=999 WHERE frame_idx=1")
    payload = service.phase_summary()
    phase, metrics = selected(payload)
    assert "motion_error" in payload["diagnostics"]
    assert all(not row["available"] and row["value"] is None for row in metrics.values())
    assert phase["safety"]["critical_structure_violation"]["count"] is None
    assert phase["safety"]["drill_visibility"]["duration_s"] is None


@pytest.mark.parametrize("damage", ["missing", "malformed", "changed_content"])
def test_invalid_reference_preserves_current_measurements(tmp_path, damage):
    service, rows, _, _ = bound_case(tmp_path)
    reference = saved_reference(service, rows)
    if damage == "missing":
        service.phase_reference_path = tmp_path / "missing_reference.json"
    elif damage == "malformed":
        service.phase_reference_path.write_text("{", encoding="utf-8")
    else:
        reference["participant_count"] = 99
        dump(service.phase_reference_path, reference)
    payload = service.phase_summary()
    _, metrics = selected(payload)
    assert "reference_error" in payload["diagnostics"]
    assert metrics["tool_speed_mm_s"]["available"]
    assert metrics["tool_speed_mm_s"]["value"] == pytest.approx(1)
    assert not metrics["tool_speed_mm_s"]["reference"]["available"]


def test_saved_reference_needs_no_expert_source_files(tmp_path, monkeypatch):
    service, rows, _, _ = bound_case(tmp_path)
    saved_reference(service, rows)
    original_open = Path.open

    def reject_expert_source(path, *args, **kwargs):
        if path.name.startswith("expert_source_"):
            pytest.fail("Runtime reopened an expert source CSV")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_expert_source)
    payload = service.phase_summary()
    _, metrics = selected(payload)
    comparison = metrics["tool_speed_mm_s"]["reference"]
    assert comparison["available"]
    assert comparison["participant_n"] == comparison["recording_n"] == 3
    assert comparison["median"] == pytest.approx(4)
    assert metrics["tool_speed_mm_s"]["value"] == pytest.approx(1)


def test_summary_counts_all_canonical_events_beyond_coaching_retrieval_limit(tmp_path, monkeypatch):
    service, _, _, _ = bound_case(tmp_path)
    with sqlite3.connect(service.retriever.db_path) as connection:
        connection.executemany(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (f"event:{i}", "synthetic:case", "simulator_overshoot", 1, 1.1,
                 "phase:antrum", "FacialNerve", "simulator_supported", "closed", "test",
                 json.dumps({"coaching_eligible": True}), "test")
                for i in range(501)
            ],
        )
    # Geometry checking is independently covered; here verify complete DB aggregation.
    monkeypatch.setattr(service, "_summary_safety_available", lambda *args: True)
    phase, _ = selected(service.phase_summary())
    safety = phase["safety"]["critical_structure_violation"]
    assert safety["available"] and safety["count"] == 501
    assert len(safety["event_ids"]) == 501


@pytest.mark.parametrize("route", ["/summary/", "/summary/app.js", "/api/phase-summary"])
def test_summary_routes_load_for_recommendations(tmp_path, route):
    recommendations = make_service(tmp_path / "recommendations")
    status, headers, body = request(recommendations, route)
    assert status == 200
    if route == "/api/phase-summary":
        assert json.loads(body)["schema"] == "phase_summary_v1"
        assert headers["Cache-Control"] == "no-store"
    else:
        assert body


@pytest.mark.parametrize("query", ["", "?participant_id=not_started"])
def test_study_summary_requires_started_and_analyzed_session(tmp_path, query):
    study = make_service(tmp_path / "study", mode="expert-study")
    status, _, body = request(study, "/api/phase-summary" + query)
    assert status in {400, 404}
    assert "error" in json.loads(body)
