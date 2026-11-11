"""Background questionnaire served while the analysis runs (FIVRS resident study).

The launcher starts the intake server when the trial ends, then replaces it with the
full expert-study server on the same participant directory once the analysis is done.
"""

import pytest
from test_ray_feedback_runtime import make_service
from test_review_view_selection import post

from evidence_system import study_surveys
from evidence_system.web import INTAKE_RUN_ID, IntakeService

PARTICIPANT = "S005"


@pytest.fixture(autouse=True)
def _study_default_protocol(monkeypatch):
    # The resident study runs without self-assessments.
    monkeypatch.setattr(study_surveys, "SELF_ASSESSMENTS", False)


def background():
    return {
        "role": "fellow",
        "training_stage": {"status": "provided", "value": 7, "unit": "pgy"},
        "cortical_mastoidectomies": "6_10",
        "vr_experience": "occasionally",
    }


def intake(tmp_path):
    return IntakeService(study_state_path=tmp_path / "participants", display_name=PARTICIPANT,
                         interface_version="expert-feedback-v1")


def start(service, view="summary"):
    status, value = post(service, "/api/expert-study/start",
                         {"participant_id": PARTICIPANT, "first_review_view": view})
    assert status == 200, value
    return value


def submit(service, flow, answers):
    return post(service, "/api/expert-study/review-next", {
        "participant_id": PARTICIPANT, "stage": flow["stage"], "view": flow["current_view"],
        "step_id": flow["step_id"], "answers": answers,
    })


def test_intake_collects_background_then_reports_pending(tmp_path):
    service = intake(tmp_path)
    value = start(service)
    assert value["intake"] and value["review_flow"]["stage"] == "background"
    assert "temporal_bone_lab_years" not in value["surveys"]["background"]["fields"]
    status, result = submit(service, value["review_flow"], background())
    assert status == 200, result
    assert result["review_flow"]["stage"] == "baseline"
    status, pending = post(service, "/api/expert-study/analyze", {"participant_id": PARTICIPANT})
    assert status == 200 and pending["pending"]
    # Baseline belongs to the real recording, never the temporary intake run.
    status, result = submit(service, result["review_flow"], {"overall_performance": 4})
    assert status == 400 and "still being prepared" in result["error"]


@pytest.mark.parametrize("view", ["event-based", "summary"])
def test_full_server_skips_the_background_answered_during_the_analysis(tmp_path, view):
    early = intake(tmp_path)
    status, result = submit(early, start(early, view)["review_flow"], background())
    assert status == 200, result
    with early._study()._locked(PARTICIPANT):
        original = early._study()._read_unlocked(PARTICIPANT)["expert_study"][
            "participants"
        ][PARTICIPANT]["background"]
    full = make_service(tmp_path, mode="expert-study")
    assert full._study().participant_path(PARTICIPANT) == (
        early._study().participant_path(PARTICIPANT)
    )
    value = start(full, view)
    assert value["review_flow"]["stage"] == "baseline"
    assert value["review_flow"]["current_view"] is None
    status, result = submit(full, value["review_flow"], {"overall_performance": 4})
    assert status == 400 and "automatic analysis" in result["error"]
    full.analyze_study(PARTICIPANT)
    status, result = submit(full, value["review_flow"], {"overall_performance": 4})
    assert status == 200, result
    assert result["review_flow"]["stage"] == "review"
    assert result["review_flow"]["current_view"] == view
    with full._study()._locked(PARTICIPANT):
        stored = full._study()._read_unlocked(PARTICIPANT)["expert_study"]
    record = stored["participants"][PARTICIPANT]["background"]
    assert record == original
    assert record["answers"] == background()
    assert record["run_id"] == INTAKE_RUN_ID
    assert set(stored["sessions"]) == {
        f"{PARTICIPANT}::{INTAKE_RUN_ID}", f"{PARTICIPANT}::case",
    }


def test_reopening_the_intake_after_submitting_does_not_ask_again(tmp_path):
    service = intake(tmp_path)
    submit(service, start(service)["review_flow"], background())
    again = intake(tmp_path)
    assert start(again)["review_flow"]["stage"] == "baseline"
