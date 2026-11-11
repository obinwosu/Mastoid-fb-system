"""Versioned VR/background and three independent overall-performance ratings."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.study_surveys import (
    SURVEY_DEFINITION,
    new_review_flow,
    validate_answers,
    validate_background,
)
from evidence_system.web import ExpertStudyStore


def background():
    return {
        "role": "resident",
        "training_stage": {"status": "provided", "value": 2, "unit": "pgy"},
        "cortical_mastoidectomies": "1_5",
        "vr_experience": "never",
    }


def feedback(overall=4):
    return {"clarity": 0, "actionability": 4, "new_insight": 3,
            "overall_performance": overall}


def advance(store, run_id, flow, answers=None):
    return store.advance_review(
        participant_id="REFLECTION-TEST", run_id=run_id,
        stage=flow["stage"], view=flow["current_view"], step_id=flow["step_id"], answers=answers,
    )


def start(store, run_id="case", **kwargs):
    return store.start_session(
        participant_id="REFLECTION-TEST", run_id=run_id, stimulus_ids=[], **kwargs,
    )["review_flow"]


def ready(store, run_id="case"):
    store.add_event(participant_id="REFLECTION-TEST", run_id=run_id,
                    event_type="analysis_completed")


@pytest.mark.parametrize("choice", ["never", "occasionally", "frequently"])
def test_vr_has_one_explicit_required_answer(choice):
    answers = {**background(), "vr_experience": choice}
    assert validate_background(answers) == answers


@pytest.mark.parametrize("choice", [True, False, None, 0, "", [], {}, "yes", "no", "not_sure"])
def test_vr_rejects_implicit_or_malformed_answers(choice):
    with pytest.raises(ValueError):
        validate_background({**background(), "vr_experience": choice})


@pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
def test_overall_scale_matches_before_and_after_feedback(rating):
    assert validate_answers("baseline", {"overall_performance": rating}) == {
        "overall_performance": rating,
    }
    assert validate_answers("questionnaire", feedback(rating)) == feedback(rating)
    baseline = SURVEY_DEFINITION["baseline"]["items"][0]
    post = SURVEY_DEFINITION["feedback"]["items"][-1]
    assert baseline["id"] == post["id"] == "overall_performance"
    assert baseline["scale"] == post["scale"]
    assert baseline["text"] == (
        "Based on your own impression, how would you rate your overall performance "
        "in the recorded procedure?"
    )
    assert post["text"] == (
        "After reviewing this feedback, how would you now rate your overall performance "
        "in the recorded procedure?"
    )
    assert not baseline.get("missing_options")
    assert not post.get("missing_options")


@pytest.mark.parametrize("rating", [
    0, 6, True, False, 1.0, None, "1", "not_sure", "unable_to_judge", [], {},
])
@pytest.mark.parametrize("stage", ["baseline", "questionnaire"])
def test_overall_rejects_zero_coercion_and_wrong_scale(stage, rating):
    answers = {"overall_performance": rating} if stage == "baseline" else feedback(rating)
    with pytest.raises(ValueError):
        validate_answers(stage, answers)


@pytest.mark.parametrize("stage", ["baseline", "questionnaire"])
def test_previously_saved_instrument_keeps_its_explicit_missing_choice(stage):
    archived = copy.deepcopy(SURVEY_DEFINITION)
    section = "baseline" if stage == "baseline" else "feedback"
    item = next(item for item in archived[section]["items"]
                if item["id"] == "overall_performance")
    item["missing_options"] = [{"value": "unable_to_judge", "label": "Unable to judge"}]
    answers = {"overall_performance": "unable_to_judge"} if stage == "baseline" else feedback(
        "unable_to_judge",
    )
    assert validate_answers(stage, answers, archived) == answers
    with pytest.raises(ValueError):
        validate_answers(stage, answers)


@pytest.mark.parametrize("rating", [0, 1, 2, 3, 4])
def test_new_insight_uses_direct_extent_scale(rating):
    answers = {**feedback(), "new_insight": rating}
    assert validate_answers("questionnaire", answers) == answers
    definition = SURVEY_DEFINITION["feedback"]
    insight = next(item for item in definition["items"] if item["id"] == "new_insight")
    assert insight["text"] == (
        "To what extent did this feedback help you recognize aspects of your "
        "performance that you had not noticed before?"
    )
    assert insight["scale"] == [
        {"value": index, "label": label}
        for index, label in enumerate([
            "Not at all", "A little", "Somewhat", "Quite a bit", "A great deal",
        ])
    ]
    assert definition["scale"][0]["label"] == "Strongly disagree"
    assert definition["scale"][4]["label"] == "Strongly agree"
    assert all("scale" not in item for item in definition["items"][:2])


@pytest.mark.parametrize("rating", [5, -1, True, 1.0, None, "unable_to_judge"])
def test_new_insight_cannot_use_performance_or_missing_options(rating):
    with pytest.raises(ValueError):
        validate_answers("questionnaire", {**feedback(), "new_insight": rating})


@pytest.mark.parametrize("first", ["event-based", "summary"])
def test_three_ratings_persist_separately_and_do_not_leak_previous_answers(tmp_path, first):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    flow = advance(store, "case", start(store, first_review_view=first), background())
    assert flow["stage"] == "baseline"
    before = store.participant_path("REFLECTION-TEST").read_bytes()
    with pytest.raises(ValueError, match="automatic analysis"):
        advance(store, "case", flow, {"overall_performance": 5})
    assert store.participant_path("REFLECTION-TEST").read_bytes() == before
    ready(store)
    flow = advance(store, "case", flow, {"overall_performance": 5})
    assert flow["stage"] == "review" and flow["current_view"] == first
    for rating in [3, 2]:
        flow = advance(store, "case", flow)
        assert flow["stage"] == "questionnaire"
        assert flow["step_id"] not in flow["responses"]
        flow = advance(store, "case", flow, feedback(rating))
    flow = advance(store, "case", flow, {"preferred_view": "combination"})
    assert flow["stage"] == "complete"
    records = flow["responses"]
    assert [records[key]["answers"]["overall_performance"] for key in
            ["baseline", "questionnaire_0", "questionnaire_1"]] == [5, 3, 2]
    for key in ["baseline", "questionnaire_0", "questionnaire_1"]:
        assert records[key]["instrument_version"] == "mastoid_surveys_v5_1_clarity"
        assert records[key]["run_id"] == "case"


def v3_flow(stage):
    """A saved minimal v3 session, without the new VR/baseline/post questions."""
    flow = new_review_flow("event-based", "old-time", background=True, final_case=True)
    definition = flow["survey_definition"]
    flow["survey_version"] = definition["version"] = "mastoid_surveys_v3_resident"
    definition.pop("baseline")
    definition["background"]["fields"].pop("vr_experience")
    definition["feedback"]["items"] = definition["feedback"]["items"][:2]
    for key in ["overall_performance_status", "overall_performance_timepoints"]:
        definition["protocol"].pop(key)
    flow["steps"] = [step for step in flow["steps"] if step["stage"] != "baseline"]
    flow["cursor"] = next(i for i, step in enumerate(flow["steps"]) if step["stage"] == stage)
    flow.update(flow["steps"][flow["cursor"]])
    return flow


@pytest.mark.parametrize("stage", ["background", "questionnaire"])
def test_saved_v3_session_validates_its_own_instrument_and_does_not_add_steps(tmp_path, stage):
    store = ExpertStudyStore(tmp_path / "participants", "new-ui")
    start(store)
    old_flow = v3_flow(stage)
    with store._locked("REFLECTION-TEST"):
        payload = store._read_unlocked("REFLECTION-TEST")
        row = payload["expert_study"]["sessions"]["REFLECTION-TEST::case"]
        row["review_flow"] = copy.deepcopy(old_flow)
        row["analysis_completed_at"] = "old-analysis"
        store._write_unlocked(payload)
    resumed = start(store)
    assert resumed == old_flow
    answers = background() if stage == "background" else {"clarity": 0, "actionability": 4}
    if stage == "background":
        answers.pop("vr_experience")
    additional = {"vr_experience": "yes"} if stage == "background" else {"new_insight": 4}
    before = store.participant_path("REFLECTION-TEST").read_bytes()
    with pytest.raises(ValueError, match="exactly"):
        advance(store, "case", resumed, {**answers, **additional})
    assert store.participant_path("REFLECTION-TEST").read_bytes() == before
    flow = advance(store, "case", resumed, answers)
    assert flow["survey_definition"] == old_flow["survey_definition"]
    assert all(step["stage"] != "baseline" for step in flow["steps"])
    record = flow["responses"][old_flow["step_id"]]
    assert record["instrument_version"] == "mastoid_surveys_v3_resident"
    assert record["answers"] == answers


def test_old_shared_background_is_not_replaced_and_each_new_case_has_own_baseline(tmp_path):
    store = ExpertStudyStore(tmp_path / "participants", "new-ui")
    old_background = {"instrument_version": "mastoid_surveys_v3_resident",
                      "answers": {key: value for key, value in background().items()
                                  if key != "vr_experience"}}
    with store._locked("REFLECTION-TEST"):
        payload = store._read_unlocked("REFLECTION-TEST")
        payload["expert_study"]["participants"]["REFLECTION-TEST"] = {
            "participant_id": "REFLECTION-TEST", "background": copy.deepcopy(old_background),
        }
        store._write_unlocked(payload)
    for case, rating in [("case-a", 2), ("case-b", 4)]:
        flow = start(store, case)
        assert flow["stage"] == "baseline"
        assert flow["responses"] == {}
        ready(store, case)
        advance(store, case, flow, {"overall_performance": rating})
    saved = json.loads(store.participant_path("REFLECTION-TEST").read_text(encoding="utf-8"))
    study = saved["expert_study"]
    assert study["participants"]["REFLECTION-TEST"]["background"] == old_background
    assert [study["sessions"][f"REFLECTION-TEST::{case}"]["review_flow"]["responses"]
            ["baseline"]["answers"]["overall_performance"] for case in
            ["case-a", "case-b"]] == [2, 4]


@pytest.mark.parametrize("vr_answer", ["yes", "no", "not_sure"])
def test_saved_v4_semantics_progress_and_answers_survive_v5(tmp_path, vr_answer):
    store = ExpertStudyStore(tmp_path / "participants", "new-ui")
    flow = start(store)
    definition = flow["survey_definition"]
    flow["survey_version"] = definition["version"] = "mastoid_surveys_v4_reflection"
    definition["background"]["fields"]["vr_experience"] = {
        "label": "Have you used a virtual reality (VR) headset before today?",
        "options": ["yes", "no", "not_sure"],
        "option_labels": ["Yes", "No", "Not sure"],
    }
    insight = next(item for item in definition["feedback"]["items"]
                   if item["id"] == "new_insight")
    insight["text"] = (
        "This feedback helped me recognize aspects of my performance "
        "that I had not noticed before."
    )
    insight.pop("scale")
    original_definition = copy.deepcopy(definition)
    with store._locked("REFLECTION-TEST"):
        payload = store._read_unlocked("REFLECTION-TEST")
        payload["expert_study"]["sessions"]["REFLECTION-TEST::case"]["review_flow"] = flow
        store._write_unlocked(payload)
    flow = start(store)
    assert flow["survey_definition"] == original_definition
    before = store.participant_path("REFLECTION-TEST").read_bytes()
    with pytest.raises(ValueError):
        advance(store, "case", flow, background())
    assert store.participant_path("REFLECTION-TEST").read_bytes() == before
    old_background = {**background(), "vr_experience": vr_answer}
    flow = advance(store, "case", flow, old_background)
    ready(store)
    flow = advance(store, "case", flow, {"overall_performance": 4})
    flow = advance(store, "case", flow)
    flow = advance(store, "case", flow, feedback(3))
    flow = advance(store, "case", flow)
    assert flow["step_id"] == "questionnaire_1"
    prior_responses = copy.deepcopy(flow["responses"])
    resumed = start(store)
    assert resumed == flow
    flow = advance(store, "case", resumed, {**feedback(2), "new_insight": 4})
    for key, value in prior_responses.items():
        assert flow["responses"][key] == value
    for key in ["questionnaire_0", "questionnaire_1"]:
        record = flow["responses"][key]
        assert record["instrument_version"] == "mastoid_surveys_v4_reflection"
        old_section = record["instrument"]["feedback"]
        assert old_section == original_definition["feedback"]
        assert old_section["scale"][3]["label"] == "Agree"
        assert "scale" not in old_section["items"][2]
    fresh_case = start(store, "second-recording")
    assert fresh_case["survey_version"] == "mastoid_surveys_v5_1_clarity"
    assert fresh_case["stage"] == "baseline"
    payload = json.loads(store.participant_path("REFLECTION-TEST").read_text(encoding="utf-8"))
    background_record = payload["expert_study"]["participants"]["REFLECTION-TEST"]["background"]
    assert background_record["instrument_version"] == "mastoid_surveys_v4_reflection"
    assert background_record["answers"] == old_background
