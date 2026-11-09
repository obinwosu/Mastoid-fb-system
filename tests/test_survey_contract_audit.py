"""Independent HTTP/storage checks for the participant survey study contract."""

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from test_ray_feedback_runtime import make_service
from test_review_view_selection import post

from evidence_system.web import ExpertStudyStore, FeedbackService

PARTICIPANT = "SURVEY-AUDIT"
ITEMS = ("protection", "visibility", "control", "removal", "orientation", "planning")



@pytest.fixture(autouse=True)
def _with_self_assessments(monkeypatch):
    """These tests cover the full protocol, kept for when self-assessments return."""
    from evidence_system import study_surveys
    monkeypatch.setattr(study_surveys, "SELF_ASSESSMENTS", True)


def background():
    return {
        "role": "resident",
        "training_stage": {"status": "provided", "value": 3, "unit": "pgy"},
        "cortical_mastoidectomies": "1_5",
    }


def assessment(round_number):
    values = dict.fromkeys(ITEMS, round_number + 1)
    if round_number == 1:
        values["orientation"] = "cannot_assess"
    if round_number == 2:
        values["planning"] = "not_applicable"
    return values


def payload_for(flow):
    if flow["stage"] == "background":
        return background()
    if flow["stage"] == "self_assessment":
        return assessment(flow["assessment_round"])
    if flow["stage"] == "questionnaire":
        return {"clarity": 0 if flow["index"] == 0 else 4, "actionability": 4}
    if flow["stage"] == "preference":
        return {"preferred_view": "no_preference"}
    return None


def advance(service, flow, answers=None, *, participant=PARTICIPANT):
    return post(service, "/api/expert-study/review-next", {
        "participant_id": participant,
        "stage": flow["stage"],
        "view": flow["current_view"],
        "step_id": flow["step_id"],
        "answers": answers,
    })


def saved(service):
    return json.loads(service._study().path.read_text(encoding="utf-8"))["expert_study"]


def response_fields(value):
    if isinstance(value, dict):
        if "answers" in value:
            yield value
        for child in value.values():
            yield from response_fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from response_fields(child)


def reopen(service):
    return FeedbackService(
        mode="expert-study", db_path=service.retriever.db_path, run_id=service.run_id,
        video_path=service.video_path, video_manifest_path=service.video_manifest_path,
        ray_v3_dir=service.ray_v3_dir, cohort_benchmark_path=service.cohort_benchmark_path,
        available_runs=service.available_runs, display_name=service.display_name,
        interface_version=service.interface_version, study_state_path=service._study().path,
        phase_reference_path=service.phase_reference_path,
    )


def finish(service, flow):
    seen = []
    while flow["stage"] != "complete":
        seen.append(flow["stage"])
        status, result = advance(service, flow, payload_for(flow))
        assert status == 200, result
        flow = result["review_flow"]
    return flow, seen


@pytest.mark.parametrize("first", ["event-based", "summary"])
def test_all_surveys_persist_exact_instrument_and_independent_rounds(tmp_path, first):
    service = make_service(tmp_path, mode="expert-study")
    evidence_before = service.retriever.db_path.read_bytes()
    start = service.start_study(PARTICIPANT, first)
    instrument = deepcopy(start["surveys"])
    assert instrument["version"] == "mastoid_surveys_v3_resident"
    for key in (
        "user_review_performed", "clinician_review_performed",
        "total_score_defined", "pass_threshold_defined",
    ):
        assert instrument["protocol"][key] is False
    assert [item["id"] for item in instrument["self_assessment"]["items"]] == list(ITEMS)
    assert not start["case_completed"]
    assert start["completion_code"] is None
    flow = start["review_flow"]
    assert flow["schema"] == "sequential_review_v2"
    order = [first, "summary" if first == "event-based" else "event-based"]
    assert flow["order"] == order
    expected = [
        ("background", None, None),
        ("self_assessment", None, 0),
        ("review", first, None),
        ("self_assessment", first, 1),
        ("questionnaire", first, None),
        ("review", order[1], None),
        ("self_assessment", order[1], 2),
        ("questionnaire", order[1], None),
        ("preference", None, None),
    ]
    submitted = {}
    for stage, view, round_number in expected:
        assert (flow["stage"], flow["current_view"], flow.get("assessment_round")) == (
            stage, view, round_number,
        )
        assert not list(response_fields(flow))
        answers = payload_for(flow)
        previous = deepcopy(flow)
        status, result = advance(service, previous, answers)
        assert status == 200, result
        if answers is not None:
            submitted[previous["step_id"]] = deepcopy(answers)
        flow = result["review_flow"]
        assert not list(response_fields(flow))
        if stage == "background":
            service.analyze_study(PARTICIPANT)
        # A stale click cannot submit another answer or advance a second time.
        before = service._study().path.read_bytes()
        assert advance(service, previous, answers)[0] == 400
        assert service._study().path.read_bytes() == before
        service = reopen(service)
        resumed = service.start_study(PARTICIPANT, order[1])
        assert resumed["review_flow"] == flow
        assert not list(response_fields(resumed))
        assert bool(resumed["completion_code"]) == (flow["stage"] == "complete")
    assert flow["stage"] == "complete"
    assert service.retriever.db_path.read_bytes() == evidence_before
    stored = saved(service)
    session = stored["sessions"][PARTICIPANT + "::case"]
    records = session["review_flow"]["responses"]
    assert set(records) == set(submitted)
    for step_id, record in records.items():
        assert record["answers"] == submitted[step_id]
        section = "feedback" if record["stage"] == "questionnaire" else record["stage"]
        expected_instrument = {
            "version": instrument["version"], "protocol": instrument["protocol"],
            section: instrument[section],
        }
        assert record["instrument"] == expected_instrument
        assert record["instrument_version"] == instrument["version"]
        encoded = json.dumps(
            expected_instrument, sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")
        assert record["instrument_sha256"] == hashlib.sha256(encoded).hexdigest()
        assert record["participant_id"] == PARTICIPANT and record["run_id"] == "case"
        assert record["step_id"] == step_id and record["submitted_at"]
        assert record["order"] == order
    sa = {r["assessment_round"]: r for r in records.values()
          if r["stage"] == "self_assessment"}
    assert sa[1]["answers"]["orientation"] == "cannot_assess"
    assert sa[2]["answers"]["planning"] == "not_applicable"
    profile = stored["participants"][PARTICIPANT]
    assert profile["background"]["answers"]["cortical_mastoidectomies"] == "1_5"
    assert profile["background"]["answers"]["training_stage"]["value"] == 3
    assert profile["preference"]["answers"] == {"preferred_view": "no_preference"}


@pytest.mark.parametrize("field,value", [
    ("training_stage", True),
    ("training_stage", -1),
    ("training_stage", float("nan")),
    ("training_stage", float("inf")),
    ("training_stage", 0.5),
    ("training_stage", 6),
])
def test_invalid_background_measurements_never_write(tmp_path, field, value):
    service = make_service(tmp_path, mode="expert-study")
    flow = service.start_study(PARTICIPANT)["review_flow"]
    answers = background()
    answers[field]["value"] = value
    before = service._study().path.read_bytes()
    status, result = advance(service, flow, answers)
    assert status == 400 and "error" in result
    assert service._study().path.read_bytes() == before


@pytest.mark.parametrize("bad_answers", [
    {**assessment(0), "protection": True},
    {**assessment(0), "protection": 0},
    {**assessment(0), "protection": 6},
    {**assessment(0), "protection": 1.5},
    {**assessment(0), "protection": "1"},
    {**assessment(0), "protection": None},
    {key: value for key, value in assessment(0).items() if key != "planning"},
    {**assessment(0), "extra": 3},
])
def test_invalid_self_assessment_rejected_without_losing_background(tmp_path, bad_answers):
    service = make_service(tmp_path, mode="expert-study")
    flow = service.start_study(PARTICIPANT)["review_flow"]
    assert advance(service, flow, background())[0] == 200
    service.analyze_study(PARTICIPANT)
    flow = service.start_study(PARTICIPANT)["review_flow"]
    before = service._study().path.read_bytes()
    status, result = advance(service, flow, bad_answers)
    assert status == 400 and "error" in result
    assert service._study().path.read_bytes() == before


@pytest.mark.parametrize("mutation", [
    "status_value", "wrong_role_unit", "old_case_years", "removed_age", "count_as_number",
])
def test_background_rejects_ambiguous_or_obsolete_measurements(tmp_path, mutation):
    service = make_service(tmp_path, mode="expert-study")
    flow = service.start_study(PARTICIPANT)["review_flow"]
    answers = background()
    if mutation == "status_value":
        answers["training_stage"] = {"status": "not_sure", "value": 5, "unit": "pgy"}
    elif mutation == "wrong_role_unit":
        answers["training_stage"]["unit"] = "independent_practice_years"
    elif mutation == "old_case_years":
        answers["clinical_mastoidectomy_years"] = {"status": "provided", "value": 1}
    elif mutation == "removed_age":
        answers["age"] = 30
    else:
        answers["cortical_mastoidectomies"] = 5
    before = service._study().path.read_bytes()
    assert advance(service, flow, answers)[0] == 400
    assert service._study().path.read_bytes() == before


@pytest.mark.parametrize("bad_rating", [False, -1, 5, 2.5, "3", None])
def test_feedback_rating_bounds_are_not_osats_bounds(tmp_path, bad_rating):
    service = make_service(tmp_path, mode="expert-study")
    flow = service.start_study(PARTICIPANT)["review_flow"]
    status, result = advance(service, flow, background())
    assert status == 200
    service.analyze_study(PARTICIPANT)
    flow = result["review_flow"]
    while flow["stage"] != "questionnaire":
        status, result = advance(service, flow, payload_for(flow))
        assert status == 200, result
        flow = result["review_flow"]
    before = service._study().path.read_bytes()
    assert advance(service, flow, {"clarity": bad_rating, "actionability": 4})[0] == 400
    assert service._study().path.read_bytes() == before
    assert advance(service, flow, {"clarity": 0, "actionability": 4})[0] == 200


def test_forged_step_id_and_another_participant_cannot_submit_current_answers(tmp_path):
    service = make_service(tmp_path, mode="expert-study")
    flow = service.start_study(PARTICIPANT)["review_flow"]
    before = service._study().path.read_bytes()
    forged = {**flow, "step_id": "self_assessment_2", "stage": "self_assessment"}
    assert advance(service, forged, assessment(2))[0] == 400
    assert advance(service, flow, background(), participant="Unknown-reviewer")[0] == 400
    assert service._study().path.read_bytes() == before


def test_background_preserves_zero_without_coercion(tmp_path):
    service = make_service(tmp_path, mode="expert-study")
    flow = service.start_study(PARTICIPANT)["review_flow"]
    answers = background()
    answers.update(
        role="attending",
        training_stage={"status": "provided", "value": 0, "unit": "independent_practice_years"},
        cortical_mastoidectomies="0",
    )
    assert advance(service, flow, answers)[0] == 200
    record = saved(service)["participants"][PARTICIPANT]["background"]
    assert record["answers"] == answers


def test_preopened_second_case_resumes_after_shared_background_is_submitted(tmp_path):
    path = tmp_path / "shared-study.json"
    first = ExpertStudyStore(path, "audit")
    second = ExpertStudyStore(path, "audit")
    first_args = dict(participant_id=PARTICIPANT, run_id="first", stimulus_ids=[])
    second_args = dict(participant_id=PARTICIPANT, run_id="second", stimulus_ids=[])
    first_session = first.start_session(**first_args, final_case=False)
    second_session = second.start_session(**second_args, final_case=True)
    assert second_session["review_flow"]["stage"] == "background"
    first_flow = first_session["review_flow"]
    first.advance_review(
        participant_id=PARTICIPANT, run_id="first", stage="background", view=None,
        step_id=first_flow["step_id"], answers=background(),
    )
    profile_before = deepcopy(json.loads(path.read_text(encoding="utf-8"))["expert_study"][
        "participants"
    ][PARTICIPANT])
    resumed = second.start_session(**second_args, final_case=True)
    assert resumed["review_flow"]["stage"] == "self_assessment"
    assert resumed["review_flow"]["assessment_round"] == 0
    assert resumed["review_flow"]["responses"] == {}
    assert json.loads(path.read_text(encoding="utf-8"))["expert_study"][
        "participants"
    ][PARTICIPANT] == profile_before


def test_legacy_flow_and_earlier_responses_are_not_recast_as_new_baselines(tmp_path):
    service = make_service(tmp_path, mode="expert-study")
    old_flow = {
        "schema": "sequential_review_v1", "order": ["event-based", "summary"],
        "stage": "review", "index": 1, "current_view": "summary",
        "questionnaires": {"event-based": {
            "status": "placeholder_continued", "continued_at": "old-time", "answers": None,
        }}, "transitions": [], "created_at": "old-time", "completed_at": None,
    }
    historic = {"answers": {"feedback_usefulness": 5}, "submitted_at": "earlier-time"}
    path = service._study().path
    path.write_text(json.dumps({
        "schema": "local_expert_study_state_v1",
        "expert_study": {"schema": "expert_feedback_study_v1", "events": [],
                         "expert_proposed_moments": {}, "sessions": {
            PARTICIPANT + "::case": {
                "participant_id": PARTICIPANT, "run_id": "case", "started_at": "old-time",
                "first_review_view": "event-based", "review_flow": old_flow,
                "analysis_completed_at": "old-analysis", "experience_response": historic,
            },
        }},
    }), encoding="utf-8")
    start = service.start_study(PARTICIPANT, "summary")
    assert start["legacy_survey_flow"] is True
    assert start["review_flow"] == old_flow
    session = service._study().progress(PARTICIPANT, "case")
    assert session["experience_response"] == historic
    assert session["analysis_completed_at"] == "old-analysis"
    assert "responses" not in session["review_flow"]
    assert PARTICIPANT not in saved(service).get("participants", {})


def test_two_case_services_share_background_but_only_last_case_asks_preference(tmp_path):
    first = make_service(tmp_path / "first", mode="expert-study")
    second_fixture = make_service(tmp_path / "second", mode="expert-study")
    with sqlite3.connect(second_fixture.retriever.db_path) as connection:
        connection.execute("UPDATE runs SET global_run_id='synthetic:second', run_id='second'")
    case_list = [
        {"run_id": "case", "url": "http://127.0.0.1:9001/"},
        {"run_id": "second", "url": "http://127.0.0.1:9002/"},
    ]
    first.available_runs = case_list
    second = FeedbackService(
        mode="expert-study", db_path=second_fixture.retriever.db_path, run_id="second",
        video_path=None, video_manifest_path=None, ray_v3_dir=None,
        cohort_benchmark_path=None, available_runs=case_list, display_name="Second case",
        interface_version="test", study_state_path=first._study().path,
    )
    flow = first.start_study(PARTICIPANT)["review_flow"]
    status, result = advance(first, flow, background())
    assert status == 200
    background_before = deepcopy(saved(first)["participants"][PARTICIPANT]["background"])
    first.analyze_study(PARTICIPANT)
    flow, seen_first = finish(first, result["review_flow"])
    assert "preference" not in seen_first
    first_case_before = deepcopy(saved(first)["sessions"][PARTICIPANT + "::case"])
    start = second.start_study(PARTICIPANT, "summary")
    assert start["review_flow"]["stage"] == "self_assessment"
    assert start["review_flow"]["assessment_round"] == 0
    second.analyze_study(PARTICIPANT)
    _, seen_second = finish(second, start["review_flow"])
    assert seen_second[-1] == "preference"
    end = saved(second)
    assert end["participants"][PARTICIPANT]["background"] == background_before
    assert end["sessions"][PARTICIPANT + "::case"] == first_case_before
    assert end["participants"][PARTICIPANT]["preference"]["run_id"] == "second"


def test_separate_store_instances_do_not_lose_concurrent_events(tmp_path):
    path = tmp_path / "shared-study.json"
    stores = [ExpertStudyStore(path, "audit") for _ in range(4)]
    gate = threading.Barrier(4)

    def write_events(index):
        gate.wait(timeout=10)
        for number in range(10):
            stores[index].add_event(
                participant_id=f"Audit-{index}", run_id="case", event_type="study_started",
                details={"worker": index, "number": number},
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write_events, range(4)))
    events = json.loads(path.read_text(encoding="utf-8"))["expert_study"]["events"]
    assert len(events) == 40
    assert {(e["details"]["worker"], e["details"]["number"]) for e in events} == {
        (worker, number) for worker in range(4) for number in range(10)
    }
