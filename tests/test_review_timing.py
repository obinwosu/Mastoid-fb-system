"""Review timing persistence, gap accounting and symmetric study API boundaries."""

import copy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_phase_summary_service import request
from test_ray_feedback_runtime import make_service
from test_review_view_selection import post

from evidence_system.review_timing import aggregate_step, record_snapshot
from evidence_system.web import ExpertStudyStore, IntakeService

START = 1_700_000_000_000
PARTICIPANT = "Timing-QA"


def snapshot(**changes):
    value = {
        "schema": "review_timing_v1", "visit_id": str(uuid4()),
        "step_id": "review_0", "view": "event-based", "review_index": 0,
        "sequence": 1, "started_at_ms": START, "observed_until_ms": START + 10000,
        "intervals": [[0, 4000, True, False], [4000, 7000, True, True],
                      [8000, 10000, False, False]],
        "coverage": ["moment:one"], "closed": False, "end_reason": None,
    }
    return {**value, **changes}


def ready(store, *, first="event-based"):
    store.start_session(participant_id=PARTICIPANT, run_id="case", stimulus_ids=[],
                        first_review_view=first)
    store.advance_review(participant_id=PARTICIPANT, run_id="case", view=None,
                         stage="background", step_id="background", answers={
                             "role": "resident", "training_stage": {
                                 "status": "provided", "value": 2, "unit": "pgy",
                             }, "cortical_mastoidectomies": "0", "vr_experience": "none",
                             "vr_simulation_experience": "none", "drilling_simulator_experience": "none",
                         })
    store.add_event(participant_id=PARTICIPANT, run_id="case", event_type="analysis_completed")
    store.advance_review(participant_id=PARTICIPANT, run_id="case", view=None,
                         stage="baseline", step_id="baseline",
                         answers={"overall_performance": 3})
    return store


@pytest.fixture
def store(tmp_path, monkeypatch):
    from evidence_system import study_surveys
    monkeypatch.setattr(study_surveys, "SELF_ASSESSMENTS", False)
    return ready(ExpertStudyStore(tmp_path / "participants", "timing-test"))


def save(store, value):
    return store.save_review_timing(participant_id=PARTICIPANT, run_id="case", snapshot=value)


def test_persists_independent_exposure_metrics_and_unknown_gap(store):
    before = store.progress(PARTICIPANT, "case")
    assert "review_timing" not in before
    result = save(store, snapshot())
    assert result["accepted"]
    assert result["step"]["elapsed_s"] == 10
    assert result["step"]["observed_s"] == 9
    assert result["step"]["unobserved_s"] == 1
    assert result["step"]["foreground_s"] == 7
    assert result["step"]["video_playing_s"] == 3
    assert result["step"]["review_exit_at_ms"] is None
    assert result["step"]["coverage_may_include_post_exit"] is False
    reopened = ExpertStudyStore(store.path, "new-interface")
    saved = reopened.progress(PARTICIPANT, "case")
    assert saved["review_flow"] == before["review_flow"]
    assert saved["review_timing"]["steps"]["review_0"] == result["step"]
    assert "review_1" not in saved["review_timing"]["steps"]


def test_retries_out_of_order_and_atomic_invalid_extension(store):
    value = snapshot()
    save(store, value)
    newer = {**value, "sequence": 3, "observed_until_ms": START + 12000,
             "intervals": [*value["intervals"][:-1], [8000, 12000, False, False]],
             "coverage": ["moment:one", "moment:two"]}
    save(store, newer)
    path = store.participant_path(PARTICIPANT)
    original = path.read_bytes()
    assert not save(store, newer)["accepted"]
    assert not save(store, {**value, "sequence": 2})["accepted"]
    assert path.read_bytes() == original
    for update in (
        {"started_at_ms": START - 1}, {"observed_until_ms": START + 11000},
        {"coverage": []}, {"intervals": [[0, 12000, True, True]]},
    ):
        with pytest.raises(ValueError):
            save(store, {**newer, "sequence": 4, **update})
        assert path.read_bytes() == original


def test_overlapping_tabs_union_exposure_and_resume_marks_unknown_gap(store):
    first = snapshot(intervals=[[0, 10000, True, True]])
    save(store, first)
    second = snapshot(started_at_ms=START + 5000, observed_until_ms=START + 15000,
                      intervals=[[0, 10000, True, False]], coverage=["phase:antrum"])
    result = save(store, second)["step"]
    assert result["elapsed_s"] == result["foreground_s"] == result["observed_s"] == 15
    assert result["video_playing_s"] == 10
    assert result["overlapping_observation_s"] == 5
    third = snapshot(started_at_ms=START + 30000, observed_until_ms=START + 35000,
                     intervals=[[0, 5000, True, False]], coverage=[])
    result = save(store, third)["step"]
    assert result["elapsed_s"] == 35
    assert result["foreground_s"] == result["observed_s"] == 20
    assert result["unobserved_s"] == 15
    assert result["visit_count"] == 3
    assert result["coverage"] == ["moment:one", "phase:antrum"]


@pytest.mark.parametrize("changes", [
    {"sequence": True}, {"sequence": 0}, {"review_index": True},
    {"started_at_ms": float("nan")}, {"observed_until_ms": float("inf")},
    {"started_at_ms": 10**400}, {"observed_until_ms": START - 1},
    {"visit_id": "not-a-uuid"}, {"schema": "invented"},
    {"intervals": [[0, 1000, False, True]]},
    {"intervals": [[0, 1000, 1, False]]},
    {"intervals": [[0, 10001, True, False]]},
    {"intervals": [[0, 5000, True, False], [4000, 7000, True, False]]},
    {"intervals": [[0, 0, True, False]]}, {"intervals": "wrong"},
    {"coverage": [1]}, {"coverage": "wrong"},
    {"closed": True}, {"end_reason": "next"},
    {"view": "summary"}, {"step_id": "questionnaire_0"},
    {"view": "summary", "step_id": "review_1", "review_index": 1},
])
def test_rejects_invalid_or_premature_snapshots_without_changing_file(store, changes):
    path = store.participant_path(PARTICIPANT)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save(store, snapshot(**changes))
    assert path.read_bytes() == before


def test_closed_visit_retry_is_safe_but_does_not_reopen(store):
    value = snapshot(closed=True, end_reason="pagehide")
    save(store, value)
    assert save(store, {**value, "sequence": 2})["accepted"]
    with pytest.raises(ValueError, match="reopened"):
        save(store, {**value, "sequence": 3, "closed": False, "end_reason": None})


def test_late_delivery_allowed_but_stale_tab_cannot_extend_past_exit(store):
    session = store.progress(PARTICIPANT, "case")
    value = snapshot(intervals=[[0, 10000, True, True]])
    record_snapshot(session, value, "received")
    flow = session["review_flow"]
    flow["cursor"] += 1
    flow.update(flow["steps"][flow["cursor"]])
    flow["transitions"].append({
        "from_stage": "review", "view": "event-based", "from_step_id": "review_0",
        "at": datetime.fromtimestamp((START + 5000) / 1000, UTC).isoformat(),
    })
    result = record_snapshot(session, {**value, "sequence": 2}, "late")["step"]
    assert result["elapsed_s"] == result["foreground_s"] == result["video_playing_s"] == 5
    assert result["coverage_may_include_post_exit"] is True
    raw = session["review_timing"]["visits"][value["visit_id"]]
    assert raw["observed_until_ms"] == START + 10000
    shorter = aggregate_step([snapshot(observed_until_ms=START + 2000,
                                      intervals=[[0, 2000, True, False]])], START + 5000)
    assert shorter["elapsed_s"] == 2  # Server receipt/Next latency never extends exposure.
    assert shorter["coverage_may_include_post_exit"] is False
    stale_only = aggregate_step([snapshot(started_at_ms=START + 10000,
                                         observed_until_ms=START + 20000)], START + 5000)
    assert stale_only["elapsed_s"] == stale_only["foreground_s"] == 0
    assert stale_only["coverage_may_include_post_exit"] is True


def test_advance_clamps_existing_timer_without_modifying_answers(store):
    now_ms = datetime.now(UTC).timestamp() * 1000
    value = snapshot(started_at_ms=now_ms - 2000, observed_until_ms=now_ms + 100000,
                     intervals=[[0, 102000, True, False]])
    save(store, value)
    before = copy.deepcopy(store.progress(PARTICIPANT, "case")["review_flow"]["responses"])
    store.advance_review(participant_id=PARTICIPANT, run_id="case", view="event-based",
                         stage="review", step_id="review_0")
    session = store.progress(PARTICIPANT, "case")
    assert 1 <= session["review_timing"]["steps"]["review_0"]["foreground_s"] < 10
    assert session["review_flow"]["responses"] == before


def test_legacy_flow_remains_legacy_and_accepts_only_reached_ordered_view(store):
    session = store.progress(PARTICIPANT, "case")
    session["review_flow"] = {"schema": "sequential_review_v1", "order": ["summary", "event-based"],
                              "index": 0, "stage": "review", "current_view": "summary"}
    before = copy.deepcopy(session["review_flow"])
    assert record_snapshot(session, snapshot(view="summary"), "now")["accepted"]
    assert session["review_flow"] == before
    with pytest.raises(ValueError):
        record_snapshot(session, snapshot(step_id="review_1", review_index=1), "now")


@pytest.mark.parametrize("view", ["event-based", "summary"])
def test_api_records_same_contract_for_both_views(tmp_path, monkeypatch, view):
    from evidence_system import study_surveys
    monkeypatch.setattr(study_surveys, "SELF_ASSESSMENTS", False)
    service = make_service(tmp_path, mode="expert-study")
    ready(service._study(), first=view)
    status, result = post(service, "/api/expert-study/review-timing", {
        "participant_id": PARTICIPANT, "snapshot": snapshot(view=view),
    })
    assert status == 200 and result["accepted"]
    assert result["step"]["view"] == view
    status, result = post(service, "/api/expert-study/review-timing", {
        "participant_id": "another-person", "snapshot": snapshot(view=view),
    })
    assert status == 400 and "Start" in result["error"]


def test_intake_and_readonly_mode_reject_timing(tmp_path):
    intake = IntakeService(study_state_path=tmp_path / "participants", display_name="Timing",
                           interface_version="timing-test")
    status, result = post(intake, "/api/expert-study/review-timing", {
        "participant_id": PARTICIPANT, "snapshot": snapshot(),
    })
    assert status == 400 and "still being prepared" in result["error"]
    readonly = make_service(tmp_path, mode="recommendations")
    assert post(readonly, "/api/expert-study/review-timing", {})[0] == 405


def test_clock_asset_is_served_only_with_study_app(tmp_path):
    service = make_service(tmp_path, mode="expert-study")
    status, headers, body = request(service, "/review-timing.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert b"ReviewTiming" in body
    service.mode = "recommendations"
    assert request(service, "/review-timing.js")[0] == 404
