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


def background(role="resident"):
    return {
        "role": role, "age": None, "age_status": "unanswered", "gender": None,
        "gender_self_description": "",
        "training_stage": {"status": "provided", "value": 2, "unit": ROLE_UNITS[role]},
        "otolaryngology_training_months": {
            "status": "not_applicable" if role == "medical_student" else "provided",
            "value": None if role == "medical_student" else 14,
        },
        "clinical_mastoidectomy_years": {"status": "provided", "value": 0},
        "temporal_bone_lab_years": {"status": "provided", "value": 0.5},
        "vr_headset_experience": "no", "other_surgical_simulator_experience": "not_sure",
        "this_simulator_attempts": {"status": "provided", "value": 0},
    }


@pytest.mark.parametrize("role", list(ROLE_UNITS))
def test_role_units_optional_demographics_and_zero_experience_are_preserved(role):
    answers = background(role)
    validated = validate_background(answers)
    assert validated == answers
    validated["training_stage"]["value"] = 3
    assert answers["training_stage"]["value"] == 2


@pytest.mark.parametrize("field", [
    "clinical_mastoidectomy_years", "temporal_bone_lab_years",
    "this_simulator_attempts", "otolaryngology_training_months",
])
def test_unknown_experience_stays_missing_instead_of_becoming_zero(field):
    answers = background()
    answers[field] = {"status": "not_sure", "value": None}
    assert validate_background(answers)[field] == {"status": "not_sure", "value": None}


@pytest.mark.parametrize("field,value", [
    ("age_status", []), ("age_status", {}), ("role", []),
    ("gender", []), ("gender_self_description", 42),
    ("training_stage", {"status": "provided", "value": 2, "unit": "medical_school_year"}),
    ("training_stage", {"status": "provided", "value": 1.5, "unit": "pgy"}),
    ("clinical_mastoidectomy_years", {"status": "not_sure", "value": 0}),
    ("clinical_mastoidectomy_years", {"status": "provided", "value": True}),
    ("clinical_mastoidectomy_years", {"status": "provided", "value": -1}),
    ("clinical_mastoidectomy_years", {"status": "provided", "value": float("nan")}),
    ("clinical_mastoidectomy_years", {"status": "provided", "value": float("inf")}),
    ("clinical_mastoidectomy_years", {"status": "provided", "value": 10**400}),
    ("this_simulator_attempts", {"status": "provided", "value": 1.5}),
    ("otolaryngology_training_months", {"status": "provided", "value": 2.5}),
    ("vr_headset_experience", False),
])
def test_invalid_background_values_raise_value_error(field, value):
    answers = background()
    answers[field] = value
    with pytest.raises(ValueError):
        validate_background(answers)


def test_age_gender_and_role_dependent_fields_are_consistent():
    answers = background("medical_student")
    answers["otolaryngology_training_months"] = {"status": "provided", "value": 0}
    with pytest.raises(ValueError, match="not applicable"):
        validate_background(answers)
    answers = background("attending")
    answers["training_stage"]["value"] = 0.5
    assert validate_background(answers)["training_stage"]["value"] == 0.5
    answers["age"] = 30
    with pytest.raises(ValueError, match="Age must be null"):
        validate_background(answers)
    answers["age_status"] = "provided"
    answers["gender"] = "self_describe"
    with pytest.raises(ValueError, match="self-description"):
        validate_background(answers)
    answers["gender_self_description"] = "My description"
    assert validate_background(answers)["gender_self_description"] == "My description"


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
