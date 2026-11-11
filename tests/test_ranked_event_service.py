"""Ranked review integration preserves source authority and existing consumers."""

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest
from test_review_frames import bound_case
from test_stroke_length_service import length_reference, with_strokes

from evidence_system.motion_comparison import DEFAULT_PROTOCOL


def item(payload, metric):
    return next(
        row for row in payload["event_review"]["items"]
        if row["phase"] == "session" and row["metric_key"] == metric
    )


def frame_token(clip):
    return parse_qs(urlparse(clip["keyframe_url"]).query)["clip_id"][0]


def safety_event():
    return {
        "event_id": "synthetic:violation", "event_type": "simulator_overshoot",
        "t_start_s": 1.0, "t_end_s": 2.0, "phase": "antrum", "anatomy": "FacialNerve",
        "attributes": {
            "coaching_eligible": True,
            "coaching_segments": [{
                "segment_id": "synthetic:violation:segment", "phase": "antrum",
                "t_start_s": 1.0, "t_end_s": 2.0, "min_edt_mm": -0.4,
                "anchor_t_s": 1.5, "anchor_frame_idx_global": 15,
            }],
        },
    }


def test_ranked_service_revalidates_exact_safety_frames_and_preserves_old_tokens(
    tmp_path, monkeypatch
):
    service, rows, source, _ = bound_case(tmp_path, seconds=12)
    rows = with_strokes(service, rows)
    length_reference(service, rows, protocol=DEFAULT_PROTOCOL)
    monkeypatch.setattr(service.retriever, "get_events", lambda *a, **k: {
        "events": [safety_event()],
    })
    paths = [service.retriever.db_path, service.cohort_benchmark_path, source]
    before = [path.read_bytes() for path in paths]
    summary = service.phase_summary()
    payload = service.bootstrap()

    violation = item(payload, "critical_structure_violation")["clips"][0]
    visibility = item(payload, "poor_drill_visibility")["clips"][0]
    old_visibility = next(
        row for row in payload["coaching_plan"]["items"] if row["category"] == "exposure"
    )["review_clips"][0]
    for clip in (violation, visibility, old_visibility):
        assert clip["keyframe_available"]
        assert clip["keyframe_frame_idx_global"] == 15
        assert clip["keyframe_time_s"] == 1.5
        assert service.original_review_frame(frame_token(clip)).startswith(b"\x89PNG\r\n\x1a\n")

    assert violation["observed"] == pytest.approx(0.4)
    for metric in ("speed_mm_s", "mean_stroke_length_mm"):
        selected = item(payload, metric)
        assert selected["reference"] is None
        assert selected["clips"]
        for clip in selected["clips"]:
            assert clip["phase"] == "antrum"
            assert clip["reference"]["comparison_basis"] == "same_phase_5s_windows"
            assert clip["native_end_s"] - clip["native_start_s"] == 5
            assert not clip["keyframe_available"]
            assert "keyframe_url" not in clip
            assert clip["playback_start_s"] == max(0, clip["t_start_s"] - 5)
            assert clip["playback_end_s"] == min(12, clip["t_end_s"] + 5)

    again = service.bootstrap()["event_review"]
    assert again == payload["event_review"]
    assert service.phase_summary() == summary
    assert [path.read_bytes() for path in paths] == before


def test_ranked_service_missing_reference_keeps_safety_and_explicit_empty_metrics(tmp_path):
    service, _, _, _ = bound_case(tmp_path)
    payload = service.bootstrap()
    assert len(payload["event_review"]["phases"]) == 3
    assert payload["event_review"]["schema"] == "session_event_review_v2"
    assert payload["event_review"]["policy"]["top_n"] == 4
    assert len(payload["event_review"]["items"]) == 5
    assert len({row["feedback_id"] for row in payload["event_review"]["items"]}) == 5
    assert item(payload, "poor_drill_visibility")["clips"]
    for row in payload["event_review"]["items"]:
        if row["category"] == "motion":
            assert row["status"] == "unavailable"
            assert row["reason"] == "current_motion_unavailable"
            assert row["reference"] is None and row["clips"] == []


def test_ranked_service_source_damage_revokes_native_keyframe_without_fabricating_one(tmp_path):
    service, _, _, stages = bound_case(tmp_path)
    selected = item(service.bootstrap(), "poor_drill_visibility")["clips"][0]
    token = frame_token(selected)
    (stages / "03_phase_detection/manifest.json").write_text("{}", encoding="utf-8")
    damaged = item(service.bootstrap(), "poor_drill_visibility")["clips"][0]
    assert not damaged["keyframe_available"]
    assert "keyframe_url" not in damaged
    with pytest.raises(ValueError, match="unavailable"):
        service.original_review_frame(token)


def test_ranked_and_legacy_interactions_use_served_ids_without_rebuilding(tmp_path, monkeypatch):
    service, _, _, _ = bound_case(tmp_path, mode="expert-study")
    participant = "preview-reviewer"
    service.start_study(participant)
    payload = service.bootstrap()
    old_id = payload["coaching_plan"]["items"][0]["feedback_id"]
    ranked_id = item(payload, "poor_drill_visibility")["feedback_id"]
    inventory = service._study().progress(participant, service.run_id)["stimulus_ids"]
    assert old_id in inventory and ranked_id in inventory

    def unexpected_rebuild():
        pytest.fail("Interaction logging must not rebuild the served analysis")

    monkeypatch.setattr(service, "bootstrap", unexpected_rebuild)
    for stimulus_id in [ranked_id, old_id, ranked_id]:
        assert service.log_event(
            participant, stimulus_id, "feedback_item_opened", {"source": "test"}
        )["ok"]
    path = service._study().participant_path(participant)
    before = path.read_bytes()
    for forged in ["event_review:antrum:invented", "event_review:unknown:poor_drill_visibility"]:
        with pytest.raises(KeyError):
            service.log_event(participant, forged, "feedback_item_opened", {})
        assert path.read_bytes() == before
    saved = json.loads(before)["expert_study"]["events"]
    assert [row["stimulus_id"] for row in saved[-3:]] == [ranked_id, old_id, ranked_id]


def test_cold_interaction_inventory_initializes_once_for_concurrent_requests(tmp_path, monkeypatch):
    service, _, _, _ = bound_case(tmp_path, mode="expert-study")
    original = service.bootstrap
    calls = []

    def counted_bootstrap():
        calls.append(True)
        return original()

    monkeypatch.setattr(service, "bootstrap", counted_bootstrap)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.log_event(
            "preview-reviewer", "event_review:session:poor_drill_visibility",
            "feedback_item_opened", {},
        ), range(2)))
    assert all(row["ok"] for row in results)
    assert len(calls) == 1


def test_reopening_study_retains_historical_inventory_and_flow(tmp_path):
    service, _, _, _ = bound_case(tmp_path, mode="expert-study")
    participant = "preview-reviewer"
    service.start_study(participant)
    store = service._study()
    first = store.progress(participant, service.run_id)
    historical = "historical:previously-served-item"
    store.start_session(
        participant_id=participant, run_id=service.run_id,
        stimulus_ids=[*first["stimulus_ids"], historical],
    )
    service.start_study(participant)
    reopened = store.progress(participant, service.run_id)
    assert set(first["stimulus_ids"]) | {historical} == set(reopened["stimulus_ids"])
    assert len(reopened["stimulus_ids"]) == len(set(reopened["stimulus_ids"]))
    for key in ("started_at", "review_flow", "interface_version_at_start", "first_review_view"):
        assert reopened[key] == first[key]
    # Historical storage is not an authorization source for arbitrary new events.
    with pytest.raises(KeyError):
        service.log_event(participant, historical, "feedback_item_opened", {})
