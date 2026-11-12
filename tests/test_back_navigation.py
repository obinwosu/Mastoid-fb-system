"""Back goes one step within a stage of the study, never back past feedback."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_survey_reflection import advance, background, feedback, ready, start

from evidence_system.study_surveys import public_review_flow
from evidence_system.web import ExpertStudyStore

PID = "REFLECTION-TEST"


def back(store, flow, run_id="case"):
    return store.go_back_review(participant_id=PID, run_id=run_id, step_id=flow["step_id"])


def walk(store, first="summary"):
    """Every step of a fresh session, as (flow at that step) in order."""
    flows = [start(store, first_review_view=first)]
    flows.append(advance(store, "case", flows[-1], background()))
    ready(store)
    flows.append(advance(store, "case", flows[-1], {"overall_performance": 3}))
    flows.append(advance(store, "case", flows[-1]))                      # questionnaire_0
    flows.append(advance(store, "case", flows[-1], feedback(2)))         # review_1
    flows.append(advance(store, "case", flows[-1]))                      # questionnaire_1
    flows.append(advance(store, "case", flows[-1], feedback(4)))         # preference
    return flows


def test_back_is_offered_only_within_a_step(tmp_path):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    stages = {}
    for flow in walk(store):
        stages[flow["step_id"]] = public_review_flow(flow)["can_go_back"]
    assert stages == {
        "background": False, "baseline": True, "review_0": False,
        "questionnaire_0": True, "review_1": False, "questionnaire_1": True,
        "preference": True,
    }


@pytest.mark.parametrize("step_id", ["background", "review_0", "review_1"])
def test_back_refused_where_not_offered_and_nothing_written(tmp_path, step_id):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    current = start(store, first_review_view="summary")
    if step_id != "background":
        current = advance(store, "case", current, background())
        ready(store)
        current = advance(store, "case", current, {"overall_performance": 3})
        if step_id == "review_1":
            current = advance(store, "case", current)
            current = advance(store, "case", current, feedback(2))
    assert current["step_id"] == step_id
    before = store.participant_path(PID).read_bytes()
    with pytest.raises(ValueError, match="not available"):
        back(store, current)
    assert store.participant_path(PID).read_bytes() == before


def test_back_withdraws_saved_answers_and_offers_them_again(tmp_path):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    flow = walk(store)[-1]
    assert flow["stage"] == "preference"
    flow = back(store, flow)
    assert flow["step_id"] == "questionnaire_1"
    assert "questionnaire_1" not in flow["responses"]
    public = public_review_flow(flow)
    assert public["previous_answers"] == feedback(4)
    assert public["revisit"] is True
    withdrawn = flow["withdrawn_responses"][-1]
    assert withdrawn["step_id"] == "questionnaire_1"
    assert withdrawn["withdrawn_reason"] == "participant_back"
    # Back again reaches the feedback page, which is not re-gated (revisit) and has no Back.
    flow = back(store, flow)
    assert flow["step_id"] == "review_1"
    public = public_review_flow(flow)
    assert public["revisit"] is True and public["can_go_back"] is False
    # Forward again and resubmit with a changed answer.
    flow = advance(store, "case", flow)
    flow = advance(store, "case", flow, feedback(5))
    assert flow["responses"]["questionnaire_1"]["answers"]["overall_performance"] == 5
    flow = advance(store, "case", flow, {"preferred_view": "combination"})
    assert flow["stage"] == "closing"
    flow = advance(store, "case", flow, {"practice_interest": "yes", "comments": ""})
    assert flow["stage"] == "complete"
    reasons = [row.get("reason") for row in flow["transitions"]]
    assert reasons.count("participant_back") == 2


def test_back_to_background_withdraws_shared_profile_answer(tmp_path):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    flow = advance(store, "case", start(store, first_review_view="summary"), background())
    ready(store)
    assert flow["stage"] == "baseline"
    flow = back(store, flow)
    assert flow["stage"] == "background"
    assert public_review_flow(flow)["previous_answers"] == background()
    changed = {**background(), "vr_experience": "extensive"}
    flow = advance(store, "case", flow, changed)
    assert flow["stage"] == "baseline"
    assert flow["responses"]["background"]["answers"]["vr_experience"] == "extensive"


def test_stale_back_is_refused(tmp_path):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    flows = walk(store)
    with pytest.raises(ValueError, match="changed"):
        back(store, flows[-2])
