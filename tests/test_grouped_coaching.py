"""One coaching card per behavior retains separate phase-specific evidence."""

import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.coaching_plan import build_session_coaching_plan
from evidence_system.motion_comparison import DEFAULT_PROTOCOL, artifact_sha256


def episode(phase, start, ordinal):
    return {
        "episode_id": f"generated:{ordinal}",
        "phase": phase,
        "phase_name": "early_surface" if phase == "early_phase" else phase,
        "phase_id": f"generated:phase:{ordinal}",
        "t_start_s": start,
        "t_end_s": start + 15,
        "native_start_s": start,
        "native_end_s": start + 15,
        "anchor_t_s": start + 1,
        "anchor_frame_idx_global": ordinal,
        "window_count": 3,
        "source_stroke_ids": [ordinal * 3 + i for i in range(3)],
    }


def comparison(speed, length):
    payload = {
        "schema": "motion_comparison_v1",
        "available": True,
        "protocol": deepcopy(DEFAULT_PROTOCOL),
        "reference_summary": {"included_participant_n": 5},
        "reference_sha256": "b" * 64,
        "window_build_sha256": "c" * 64,
        "claim_boundary": "Generated numerical fixture, not clinical assessment.",
        "length_review_policy": {"persistence_windows": 3, "min_distinct_strokes": 3},
        "per_phase": {
            phase: {"speed_mm_s": {"median": 10 + i}, "mean_stroke_length_mm": {"q25": 2 + i}}
            for i, phase in enumerate(("early_phase", "antrum", "incus"))
        },
        "episodes": speed,
        "length_episodes": length,
    }
    payload["artifact_sha256"] = artifact_sha256(payload)
    return payload


def test_grouped_cards_preserve_chronological_phase_intervals_and_anchors():
    episodes = [
        episode("incus", 120, 4),
        episode("early_phase", 5, 1),
        episode("antrum", 70, 3),
        episode("early_phase", 35, 2),
    ]
    payload = comparison(episodes, episodes)
    original = deepcopy(payload)
    plan = build_session_coaching_plan([], [], {}, None, motion_comparison=payload)
    assert payload == original
    cards = {item["feedback_id"]: item for item in plan["items"]}
    assert set(cards) == {"coaching:motion_reference", "coaching:short_strokes"}
    assert cards["coaching:motion_reference"]["title"] == "Review lower tool-motion speed"
    assert cards["coaching:short_strokes"]["title"] == "Review shorter strokes"
    expected = sorted(episodes, key=lambda row: row["t_start_s"])
    for card in cards.values():
        assert card["phase"] == "session"
        assert card["phases"] == ["early_phase", "antrum", "incus"]
        assert card["evidence_segments"] == expected
        assert card["source_ids"] == [row["episode_id"] for row in expected]
        assert list(card["technical_facts"]["phase_references"]) == card["phases"]
        assert (
            card["t_start_s"] == 5 and card["t_end_s"] == 20
        )  # First clip, not an invented envelope.
        assert len(card["review_clips"]) == 4
        for clip, source in zip(card["review_clips"], expected, strict=False):
            assert clip == {**source, "review_clip_id": source["episode_id"]}
    assert cards["coaching:short_strokes"]["technical_facts"]["frequency_used"] is False


def test_single_phase_still_has_generic_title_and_phase_only_on_clip():
    payload = comparison([], [episode("antrum", 50, 1)])
    (item,) = build_session_coaching_plan([], [], {}, None, motion_comparison=payload)["items"]
    assert item["feedback_id"] == "coaching:short_strokes"
    assert "during" not in item["title"] and "antrum" not in item["title"]
    assert item["review_clips"][0]["phase"] == "antrum"
    assert item["technical_facts"]["phase_references"] == {"antrum": {"q25": 3}}


def test_no_qualifying_episodes_does_not_invent_a_grouped_card():
    payload = comparison([], [])
    assert build_session_coaching_plan([], [], {}, None, motion_comparison=payload)["items"] == []


def test_length_evidence_text_uses_the_attached_trigger_policy():
    payload = comparison([], [episode("antrum", 50, 1)])
    (old,) = build_session_coaching_plan([], [], {}, None, motion_comparison=payload)["items"]
    assert (
        "3 distinct eligible complete strokes in 3 consecutive five-second windows"
        in old["evidence_summary"]
    )
    payload["length_review_policy"].update(persistence_windows=1, min_distinct_strokes=1)
    payload["artifact_sha256"] = artifact_sha256(payload)
    (current,) = build_session_coaching_plan([], [], {}, None, motion_comparison=payload)["items"]
    assert (
        "one eligible complete stroke in one qualifying five-second window"
        in current["evidence_summary"]
    )
    assert "consecutive" not in current["observed_issue"]
