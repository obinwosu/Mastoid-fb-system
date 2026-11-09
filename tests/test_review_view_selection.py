"""Study review selection preserves session identity without changing performer evidence."""

import json
import sqlite3
from io import BytesIO

import pytest
from test_phase_summary_service import request, saved_reference, selected
from test_ray_feedback_runtime import ROOT, make_service
from test_review_frames import bound_case

from evidence_system.web import ExpertStudyStore, FeedbackService, _handler


def post(service, route, payload):
    handler = object.__new__(_handler(service, ROOT / "apps/expert_study"))
    body = json.dumps(payload).encode()
    handler.path = route
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile, handler.wfile = BytesIO(body), BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.do_POST()
    return statuses[-1], json.loads(handler.wfile.getvalue())


@pytest.mark.parametrize(
    "first,subsequent", [("event-based", "summary"), ("summary", "event-based")]
)
def test_first_review_view_persists_across_resume_and_store_reload(tmp_path, first, subsequent):
    path = tmp_path / "participants"
    store = ExpertStudyStore(path, "initial-interface")
    original = store.start_session(
        participant_id="Reviewer-01", run_id="case", stimulus_ids=[], first_review_view=first
    )
    reopened = ExpertStudyStore(path, "later-interface")
    resumed = reopened.start_session(
        participant_id="Reviewer-01", run_id="case", stimulus_ids=[],
        first_review_view=subsequent,
    )
    assert resumed["first_review_view"] == first
    assert resumed["started_at"] == original["started_at"]
    assert resumed["participant_id"] == "Reviewer-01"
    assert reopened.progress("Reviewer-01", "case")["first_review_view"] == first


def test_default_first_view_is_event_based(tmp_path):
    service = make_service(tmp_path, mode="expert-study")
    status, response = post(service, "/api/expert-study/start", {"participant_id": "Doctor-02"})
    assert status == 200
    assert response["first_review_view"] == "event-based"
    assert service._study().progress("Doctor-02", "case")["first_review_view"] == "event-based"


@pytest.mark.parametrize("existing", [False, True])
def test_invalid_selection_rejected_before_state_write(tmp_path, existing):
    service = make_service(tmp_path, mode="expert-study")
    if existing:
        service.start_study("Doctor-02", "summary")
    path = service._study().participant_path("Doctor-02")
    before = path.read_bytes() if path.exists() else None
    status, response = post(service, "/api/expert-study/start", {
        "participant_id": "Doctor-02", "first_review_view": "unsupported",
    })
    assert status == 400 and "error" in response
    assert (path.read_bytes() if path.exists() else None) == before
    with pytest.raises(ValueError, match="First review view"):
        service._study().start_session(
            participant_id="Doctor-02", run_id="case", stimulus_ids=[],
            first_review_view="unsupported",
        )
    assert (path.read_bytes() if path.exists() else None) == before


def test_summary_api_requires_start_and_analysis_and_keeps_id_on_resume(tmp_path):
    service = make_service(tmp_path, mode="expert-study")
    identity = "Surgeon-A.03"
    route = "/api/phase-summary?participant_id=" + identity
    assert request(service, route)[0] == 400
    status, started = post(service, "/api/expert-study/start", {
        "participant_id": identity, "first_review_view": "summary",
    })
    assert status == 200 and started["participant_id"] == identity
    assert started["first_review_view"] == "summary"
    assert request(service, route)[0] == 400
    assert post(service, "/api/expert-study/analyze", {"participant_id": identity})[0] == 200
    status, _, body = request(service, route)
    assert status == 200
    payload = json.loads(body)
    assert payload["study"] == {"participant_id": identity, "first_review_view": "summary"}
    completion = service._study().progress(identity, "case")["analysis_completed_at"]
    status, resumed = post(service, "/api/expert-study/start", {
        "participant_id": identity, "first_review_view": "event-based",
    })
    assert status == 200 and resumed["first_review_view"] == "summary"
    assert service._study().progress(identity, "case")["analysis_completed_at"] == completion
    assert request(service, route)[0] == 200
    assert request(service, "/api/phase-summary?participant_id=another-reviewer")[0] == 400


def test_reviewer_identity_does_not_exclude_expert_performer_reference(tmp_path):
    recording, frames, _, _ = bound_case(tmp_path)
    with sqlite3.connect(recording.retriever.db_path) as connection:
        connection.execute("UPDATE runs SET participant_id='expert_b'")
    service = FeedbackService(
        mode="expert-study", db_path=recording.retriever.db_path,
        run_id=recording.run_id, video_path=None, video_manifest_path=None,
        ray_v3_dir=None, cohort_benchmark_path=None, available_runs=[],
        display_name="Synthetic recording", interface_version="test",
        study_state_path=recording.artifact_root / "participants",
    )
    saved_reference(service, frames)
    service.start_study("expert_a", "summary")
    service.analyze_study("expert_a")
    payload = service.phase_summary("expert_a")
    _, metrics = selected(payload)
    points = metrics["tool_speed_mm_s"]["reference"]["points"]
    assert {row["participant_id"] for row in points} == {"expert_a", "expert_c"}
    assert payload["reference"]["excluded_recording_ids"] == ["expert:expert_b"]
    assert payload["study"]["participant_id"] == "expert_a"


def test_review_view_opened_event_keeps_reviewer_and_selected_view(tmp_path):
    service = make_service(tmp_path, mode="expert-study")
    service.start_study("Doctor-04")
    status, response = post(service, "/api/expert-study/event", {
        "participant_id": "Doctor-04", "event_type": "review_view_opened",
        "details": {"view": "summary"},
    })
    assert status == 200 and response["ok"]
    saved = json.loads(service._study().participant_path("Doctor-04").read_text(encoding="utf-8"))
    event = saved["expert_study"]["events"][-1]
    assert event["event_type"] == "review_view_opened"
    assert event["participant_id"] == "Doctor-04" and event["run_id"] == "case"
    assert event["details"]["view"] == "summary"


@pytest.mark.parametrize("event_type", ["analysis_started", "analysis_completed"])
def test_client_event_cannot_mark_analysis_complete_or_unlock_summary(tmp_path, event_type):
    service = make_service(tmp_path, mode="expert-study")
    service.start_study("Doctor-05", "summary")
    path = service._study().participant_path("Doctor-05")
    before = path.read_bytes()
    status, response = post(service, "/api/expert-study/event", {
        "participant_id": "Doctor-05", "event_type": event_type, "details": {},
    })
    assert status == 400 and "error" in response
    assert path.read_bytes() == before
    assert "analysis_completed_at" not in service._study().progress("Doctor-05", "case")
    assert request(service, "/api/phase-summary?participant_id=Doctor-05")[0] == 400
    assert post(service, "/api/expert-study/analyze", {"participant_id": "Doctor-05"})[0] == 200
    assert request(service, "/api/phase-summary?participant_id=Doctor-05")[0] == 200


@pytest.mark.parametrize("route", ["/summary/", "/summary/app.js", "/summary/styles.css"])
def test_shared_summary_assets_are_available_in_study_mode(tmp_path, route):
    service = make_service(tmp_path, mode="expert-study")
    assert request(service, route)[0] == 200
