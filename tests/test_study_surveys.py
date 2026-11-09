"""Strict survey answers, frozen research instruments, and shared-store transactions."""

import copy
import json
import multiprocessing
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.study_surveys import (
    ROLE_UNITS,
    SELF_ASSESSMENT_ITEMS,
    SURVEY_DEFINITION,
    new_review_flow,
    response_record,
    validate_answers,
    validate_background,
)
from evidence_system.web import ExpertStudyStore



@pytest.fixture(autouse=True)
def _with_self_assessments(monkeypatch):
    """These tests cover the full protocol, kept for when self-assessments return."""
    from evidence_system import study_surveys
    monkeypatch.setattr(study_surveys, "SELF_ASSESSMENTS", True)


STAGE_FOR_ROLE = {"resident": 2, "fellow": 6, "attending": 0}


def background(role="resident"):
    return {
        "role": role,
        "training_stage": {"status": "provided", "value": STAGE_FOR_ROLE[role], "unit": ROLE_UNITS[role]},
        "cortical_mastoidectomies": "0",
    }


@pytest.mark.parametrize("role", list(ROLE_UNITS))
def test_roles_and_zero_experience_are_preserved(role):
    answers = background(role)
    validated = validate_background(answers)
    assert validated == answers
    validated["training_stage"]["value"] = 3
    assert answers["training_stage"]["value"] == STAGE_FOR_ROLE[role]


@pytest.mark.parametrize("count", ["0", "1_5", "6_10", "11_20", "over_20"])
def test_every_mastoidectomy_count_choice_is_accepted(count):
    answers = background()
    answers["cortical_mastoidectomies"] = count
    assert validate_background(answers)["cortical_mastoidectomies"] == count


@pytest.mark.parametrize("role,value", [
    ("resident", 1), ("resident", 5), ("fellow", 6), ("fellow", 7), ("attending", 0), ("attending", 25),
])
def test_each_role_accepts_its_own_years(role, value):
    answers = background(role)
    answers["training_stage"]["value"] = value
    assert validate_background(answers)["training_stage"]["value"] == value


@pytest.mark.parametrize("role,value", [
    ("resident", 0), ("resident", 6), ("fellow", 5), ("fellow", 8), ("attending", -1), ("attending", 2.5),
])
def test_each_role_rejects_years_outside_its_choices(role, value):
    answers = background(role)
    answers["training_stage"]["value"] = value
    with pytest.raises(ValueError):
        validate_background(answers)


@pytest.mark.parametrize("role", list(ROLE_UNITS))
def test_training_year_must_be_given(role):
    answers = background(role)
    answers["training_stage"] = {"status": "not_sure", "value": None, "unit": ROLE_UNITS[role]}
    with pytest.raises(ValueError):
        validate_background(answers)


@pytest.mark.parametrize("field,value", [
    ("role", []), ("role", "medical_student"),
    ("training_stage", {"status": "provided", "value": 2, "unit": "medical_school_year"}),
    ("training_stage", {"status": "provided", "value": 1.5, "unit": "pgy"}),
    ("training_stage", {"status": "provided", "value": 6, "unit": "pgy"}),
    ("cortical_mastoidectomies", 3), ("cortical_mastoidectomies", "3"),
    ("cortical_mastoidectomies", None),
    ("training_stage", {"status": "provided", "value": True, "unit": "pgy"}),
    ("training_stage", {"status": "provided", "value": float("nan"), "unit": "pgy"}),
    ("training_stage", {"status": "provided", "value": 10**400, "unit": "pgy"}),
])
def test_invalid_background_values_raise_value_error(field, value):
    answers = background()
    answers[field] = value
    with pytest.raises(ValueError):
        validate_background(answers)


def test_removed_questions_are_rejected():
    for removed in ("age", "temporal_bone_lab_years"):
        answers = background()
        answers[removed] = None
        with pytest.raises(ValueError, match="must contain exactly"):
            validate_background(answers)


@pytest.mark.parametrize("value", [True, False, 0, 6, 1.0, None, "3", {}, []])
def test_self_assessment_rejects_non_ratings(value):
    answers = {item["id"]: 3 for item in SELF_ASSESSMENT_ITEMS}
    answers["control"] = value
    with pytest.raises(ValueError):
        validate_answers("self_assessment", answers)


def test_self_assessment_missingness_is_distinct_and_never_imputed():
    answers = {item["id"]: 3 for item in SELF_ASSESSMENT_ITEMS}
    answers.update(orientation="cannot_assess", planning="not_applicable")
    assert validate_answers("self_assessment", answers) == answers
    for invalid in ({**answers, "total": 18}, {k: v for k, v in answers.items() if k != "control"}):
        with pytest.raises(ValueError):
            validate_answers("self_assessment", invalid)


@pytest.mark.parametrize("value", [True, False, -1, 5, 0.0, None, "0"])
def test_feedback_requires_integer_zero_through_four(value):
    with pytest.raises(ValueError):
        validate_answers("questionnaire", {"clarity": value, "actionability": 4})
    assert validate_answers("questionnaire", {"clarity": 0, "actionability": 4}) == {
        "clarity": 0, "actionability": 4,
    }


def test_instrument_is_frozen_for_a_session_and_response_snapshot():
    original = copy.deepcopy(SURVEY_DEFINITION)
    flow = new_review_flow("summary", "start-time", background=False, final_case=True)
    try:
        SURVEY_DEFINITION["self_assessment"] = copy.deepcopy(original["self_assessment"])
        SURVEY_DEFINITION["self_assessment"]["items"][0]["title"] = "A future wording"
        answers = {item["id"]: 3 for item in SELF_ASSESSMENT_ITEMS}
        record = response_record(flow, answers, participant_id="Reviewer", run_id="case", now="now")
        assert record["instrument"]["self_assessment"] == original["self_assessment"]
        assert record["assessment_round"] == 0
        assert record["instrument"]["protocol"]["total_score_defined"] is False
        assert record["instrument"]["protocol"]["clinician_review_performed"] is False
        flow["survey_definition"]["self_assessment"]["title"] = "Changed after submission"
        answers["control"] = 1
        assert record["answers"]["control"] == 3
        assert (
            record["instrument"]["self_assessment"]["title"]
            == original["self_assessment"]["title"]
        )
    finally:
        SURVEY_DEFINITION.clear()
        SURVEY_DEFINITION.update(original)


def _write_sessions(path, prefix):
    store = ExpertStudyStore(path, "test")
    for index in range(8):
        store.start_session(participant_id=f"{prefix}-{index}", run_id="case", stimulus_ids=[])


def test_separate_processes_do_not_lose_shared_state_transactions(tmp_path):
    path = tmp_path / "study.json"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_write_sessions, args=(path, prefix)) for prefix in ("A", "B")
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=25)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    saved = json.loads(path.read_text(encoding="utf-8"))["expert_study"]
    assert len(saved["sessions"]) == 16


def test_unrelated_state_metadata_survives_survey_transactions(tmp_path):
    path = tmp_path / "study.json"
    path.write_text(json.dumps({"custom_metadata": {"keep": True}, "expert_study": {
        "participants": {"Old": {"custom_profile": "keep"}}, "custom_study": [1, 2],
    }}), encoding="utf-8")
    ExpertStudyStore(path, "test").start_session(
        participant_id="New", run_id="case", stimulus_ids=[],
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["custom_metadata"] == {"keep": True}
    assert saved["expert_study"]["custom_study"] == [1, 2]
    assert saved["expert_study"]["participants"]["Old"] == {"custom_profile": "keep"}


def test_default_resident_study_flow_has_no_self_assessments(monkeypatch):
    from evidence_system import study_surveys
    monkeypatch.setattr(study_surveys, "SELF_ASSESSMENTS", False)
    flow = new_review_flow("summary", "start-time", background=True, final_case=True)
    stages = [step["stage"] for step in flow["steps"]]
    assert stages == ["background", "review", "questionnaire", "review", "questionnaire",
                      "preference", "complete"]


@pytest.mark.parametrize("choice", ["event-based", "summary", "combination", "no_preference"])
def test_preference_accepts_each_resident_study_option(choice):
    assert validate_answers("preference", {"preferred_view": choice}) == {"preferred_view": choice}


def test_preference_rejects_unknown_option():
    with pytest.raises(ValueError):
        validate_answers("preference", {"preferred_view": "both"})
