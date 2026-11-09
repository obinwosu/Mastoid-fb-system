"""Legacy saved two-view flows retain their original placeholder sequence."""

import json

import pytest
from test_ray_feedback_runtime import make_service as make_base_service
from test_review_view_selection import post

from evidence_system.web import FeedbackService

IDENTITY = "Reviewer-sequential"


def make_service(tmp_path, *, mode):
    service = make_base_service(tmp_path, mode=mode)
    # This file exercises saved pre-survey sessions; fresh sessions use v2.
    payload = {
        "schema": "local_expert_study_state_v1", "participant_id": IDENTITY,
        "expert_study": {
            "schema": "expert_feedback_study_v1", "sessions": {}, "participants": {},
            "events": [], "expert_proposed_moments": {},
        },
    }
    payload["expert_study"]["sessions"][IDENTITY + "::" + service.run_id] = {
        "participant_id": IDENTITY, "run_id": service.run_id,
        "started_at": "legacy-start-time",
    }
    path = service._study().participant_path(IDENTITY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return service


def reopen(service):
    return FeedbackService(
        mode="expert-study", db_path=service.retriever.db_path, run_id=service.run_id,
        video_path=service.video_path, video_manifest_path=service.video_manifest_path,
        ray_v3_dir=service.ray_v3_dir, cohort_benchmark_path=service.cohort_benchmark_path,
        available_runs=service.available_runs, display_name=service.display_name,
        interface_version=service.interface_version, study_state_path=service._study().path,
        phase_reference_path=service.phase_reference_path,
    )


def advance(service, view, stage, identity=IDENTITY):
    return post(service, "/api/expert-study/review-next", {
        "participant_id": identity, "view": view, "stage": stage,
    })


def assert_flow(payload, order, index, stage):
    flow = payload["review_flow"]
    assert flow["schema"] == "sequential_review_v1"
    assert flow["order"] == order
    assert flow["index"] == index
    assert flow["stage"] == stage
    assert flow["current_view"] == (order[index] if index < 2 else None)
    return flow


@pytest.mark.parametrize("first", ["event-based", "summary"])
def test_both_orders_resume_each_step_and_complete_only_after_second_questionnaire(tmp_path, first):
    service = make_service(tmp_path, mode="expert-study")
    evidence_before = service.retriever.db_path.read_bytes()
    order = [first, "summary" if first == "event-based" else "event-based"]
    start = service.start_study(IDENTITY, first)
    assert_flow(start, order, 0, "review")
    assert not start["case_completed"]
    service.analyze_study(IDENTITY)
    expected = [(0, "questionnaire"), (1, "review"), (1, "questionnaire"), (2, "complete")]
    current_view, current_stage = first, "review"
    for index, stage in expected:
        status, response = advance(service, current_view, current_stage)
        assert status == 200
        flow = assert_flow(response, order, index, stage)
        session = service._study().progress(IDENTITY, service.run_id)
        assert "experience_response" not in session
        if stage != "complete":
            assert session.get("case_completed_at") is None
            assert not flow.get("completed_at")
        else:
            assert session.get("case_completed_at")
            assert flow.get("completed_at")
            for view in order:
                assert flow["questionnaires"][view]["status"] == "placeholder_continued"
                assert flow["questionnaires"][view]["answers"] is None
        # Reopening the application and choosing another preference cannot restart the flow.
        service = reopen(service)
        resumed = service.start_study(IDENTITY, order[1])
        assert_flow(resumed, order, index, stage)
        assert resumed["participant_id"] == IDENTITY
        assert resumed["first_review_view"] == first
        assert resumed["case_completed"] is (stage == "complete")
        current_view, current_stage = flow["current_view"], stage
    assert service.retriever.db_path.read_bytes() == evidence_before


@pytest.mark.parametrize("started", [False, True])
def test_next_requires_started_session_and_completed_analysis_without_writing(tmp_path, started):
    factory = make_service if started else make_base_service
    service = factory(tmp_path, mode="expert-study")
    if started:
        service.start_study(IDENTITY)
    path = service._study().participant_path(IDENTITY)
    before = path.read_bytes() if path.exists() else None
    status, response = advance(service, "event-based", "review")
    assert status == 400 and "error" in response
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("view,stage", [
    ("summary", "review"), ("event-based", "questionnaire"),
    ("unsupported", "review"), ("event-based", "unsupported"),
])
def test_out_of_order_or_invalid_next_leaves_saved_flow_unchanged(tmp_path, view, stage):
    service = make_service(tmp_path, mode="expert-study")
    service.start_study(IDENTITY, "event-based")
    service.analyze_study(IDENTITY)
    path = service._study().participant_path(IDENTITY)
    before = path.read_bytes()
    status, response = advance(service, view, stage)
    assert status == 400 and "error" in response
    assert path.read_bytes() == before
    assert_flow(service.start_study(IDENTITY), ["event-based", "summary"], 0, "review")


@pytest.mark.parametrize("first", ["event-based", "summary"])
def test_duplicate_or_stale_next_never_skips_a_step(tmp_path, first):
    service = make_service(tmp_path, mode="expert-study")
    other = "summary" if first == "event-based" else "event-based"
    service.start_study(IDENTITY, first)
    service.analyze_study(IDENTITY)
    submitted = [(first, "review"), (first, "questionnaire"),
                 (other, "review"), (other, "questionnaire")]
    for view, stage in submitted:
        assert advance(service, view, stage)[0] == 200
        before = service._study().participant_path(IDENTITY).read_bytes()
        assert advance(service, view, stage)[0] == 400
        assert service._study().participant_path(IDENTITY).read_bytes() == before
    assert_flow(service.start_study(IDENTITY), [first, other], 2, "complete")


def test_legacy_responses_and_first_preference_survive_new_completion_context(tmp_path):
    service = make_service(tmp_path, mode="expert-study")
    legacy_answers = {"answers": {"usefulness": 4}, "submitted_at": "old-response-time"}
    old_completed = "old-case-completion-time"
    path = service._study().participant_path(IDENTITY)
    path.write_text(json.dumps({
        "schema": "local_expert_study_state_v1", "participant_id": IDENTITY,
        "expert_study": {"schema": "expert_feedback_study_v1", "events": [],
                         "expert_proposed_moments": {}, "sessions": {
            IDENTITY + "::case": {
                "participant_id": IDENTITY, "run_id": "case", "started_at": "old-start-time",
                "first_review_view": "summary", "case_completed_at": old_completed,
                "experience_response": legacy_answers,
            },
        }},
    }), encoding="utf-8")
    start = service.start_study(IDENTITY, "event-based")
    assert_flow(start, ["summary", "event-based"], 0, "review")
    assert start["first_review_view"] == "summary"
    assert start["case_completed"] is False
    session = service._study().progress(IDENTITY, "case")
    assert session["case_completed_at"] == old_completed
    assert session["experience_response"] == legacy_answers
    service.analyze_study(IDENTITY)
    for view, stage in [("summary", "review"), ("summary", "questionnaire"),
                        ("event-based", "review"), ("event-based", "questionnaire")]:
        assert advance(service, view, stage)[0] == 200
    session = reopen(service)._study().progress(IDENTITY, "case")
    assert session["case_completed_at"] == old_completed
    assert session["experience_response"] == legacy_answers
    assert session["review_flow"]["completed_at"] != old_completed
    assert session["review_flow"]["stage"] == "complete"


@pytest.mark.parametrize("steps", [0, 2, 4])
def test_legacy_completion_endpoint_cannot_skip_or_overwrite_sequential_flow(tmp_path, steps):
    service = make_service(tmp_path, mode="expert-study")
    service.start_study(IDENTITY, "event-based")
    service.analyze_study(IDENTITY)
    sequence = [("event-based", "review"), ("event-based", "questionnaire"),
                ("summary", "review"), ("summary", "questionnaire")]
    for view, stage in sequence[:steps]:
        assert advance(service, view, stage)[0] == 200
    # Simulate an archived response retained when this session entered the new flow.
    path = service._study().participant_path(IDENTITY)
    data = json.loads(path.read_text(encoding="utf-8"))
    prior_response = {"answers": {"workflow_clarity": 2}, "submitted_at": "legacy-response-time"}
    data["expert_study"]["sessions"][IDENTITY + "::case"]["experience_response"] = prior_response
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    status, response = post(service, "/api/expert-study/complete-experience", {
        "participant_id": IDENTITY,
        "answers": {"workflow_clarity": 5, "feedback_usefulness": 5, "debrief_fit": 5,
                    "would_use": "yes", "most_useful": "test", "missing_or_concerning": ""},
    })
    assert status == 400 and "error" in response
    assert path.read_bytes() == before
    session = reopen(service)._study().progress(IDENTITY, "case")
    assert session["experience_response"] == prior_response
    assert session["review_flow"]["stage"] == ("complete" if steps == 4 else "review")
    assert bool(session.get("case_completed_at")) is (steps == 4)
