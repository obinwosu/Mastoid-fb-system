"""Current five-second bins are disjoint and archives retain their own grid."""

from copy import deepcopy

import pytest

from test_motion_comparison import frames, provenance
from test_stroke_length_comparison import rows as stroke_rows
from test_stroke_length_service import length_reference, save_sidecar, with_strokes
from test_review_frames import bound_case
from evidence_system.coaching_plan import build_session_coaching_plan
from evidence_system.motion_comparison import (
    DEFAULT_PROTOCOL, LEGACY_PROTOCOL, add_stroke_length, build_expert_reference,
    build_motion_windows, compare_motion_windows, validate_window_build,
)


def current(source, *, run="generated:query", person="generated_query", protocol=None, length=False):
    result = build_motion_windows(source, global_run_id=run, participant_id=person,
                                  provenance=provenance(), protocol=protocol)
    return add_stroke_length(result, source) if length else result


def experts(*, length=False, protocol=None):
    builds = [current(stroke_rows(seconds=16, stroke_frames=8) if length else frames(10, seconds=16),
                      run=f"generated:expert:{i}", person=f"expert_{i}", protocol=protocol, length=length)
              for i in range(3)]
    return build_expert_reference(builds, expert_participant_ids=[f"expert_{i}" for i in range(3)],
                                  provenance={"expert_definition": "Synthetic numerical reference"})


def test_default_grid_is_nonoverlapping_and_discards_partial_tail():
    result = current(frames(1, seconds=17))
    assert result["protocol"]["schema"] == "motion_window_protocol_v2"
    assert result["protocol"]["stride_s"] == 5
    assert result["protocol"]["persistence_windows"] == 1
    assert [(w["native_start_s"], w["native_end_s"]) for w in result["windows"]] == [(0, 5), (5, 10), (10, 15)]
    assert all(w["speed_mm_s"] == pytest.approx(1) for w in result["windows"])
    assert sum(w["valid_duration_s"] for w in result["windows"]) == 15
    validate_window_build(result)


def test_nonoverlap_grid_restarts_at_each_phase_without_crossing_boundaries():
    source = []
    for phase_index, phase in enumerate(("early_surface", "antrum", "incus")):
        for row in frames(1, seconds=6.5, phase=phase):
            source.append({**row, "t_abs": row["t_abs"] + phase_index * 6.75,
                           "t_video_s": row["t_video_s"] + phase_index * 6.75})
    result = current(source)
    assert [(w["native_start_s"], w["native_end_s"]) for w in result["windows"]] == [
        (0, 5), (6.75, 11.75), (13.5, 18.5)]
    assert [w["phase"] for w in result["windows"]] == ["early_phase", "antrum", "incus"]
    assert all(source[w["source_start_row"]]["phase_id"] == source[w["source_end_row"]]["phase_id"]
               for w in result["windows"])


def test_complete_stroke_endpoint_is_assigned_to_only_one_current_bin():
    result = current(stroke_rows(seconds=16), length=True)
    identities = [sid for w in result["windows"] for sid in w["stroke_length_ids"]]
    assert len(identities) == len(set(identities)) == 15
    assert result["windows"][0]["stroke_length_ids"] == list(range(5))
    assert result["windows"][1]["stroke_length_ids"] == list(range(5, 10))
    assert result["windows"][2]["stroke_length_ids"] == list(range(10, 15))


@pytest.mark.parametrize("length", [False, True])
def test_adjacent_qualifying_current_bins_form_one_review_interval(length):
    source = stroke_rows(seconds=16) if length else frames(1, seconds=16)
    result = compare_motion_windows(current(source, length=length), experts(length=length))
    key = "length_episodes" if length else "episodes"
    assert len(result[key]) == 1
    episode = result[key][0]
    assert episode["window_count"] == 3
    assert episode["native_end_s"] - episode["native_start_s"] == 15
    plan = build_session_coaching_plan([], [], {}, None, motion_comparison=result)
    expected = "coaching:short_strokes" if length else "coaching:motion_reference"
    assert [i["feedback_id"] for i in plan["items"]] == [expected]
    partial = stroke_rows(seconds=4) if length else frames(1, seconds=4)
    assert compare_motion_windows(current(partial, length=length), experts(length=length))[key] == []


@pytest.mark.parametrize("length", [False, True])
def test_one_complete_five_second_window_triggers_review_without_persistence(length):
    source = stroke_rows(seconds=5) if length else frames(1, seconds=5)
    if length:
        # Exactly one observed complete 1-mm stroke; the terminal remainder
        # is retained but excluded from complete Length measurements.
        for i, row in enumerate(source):
            row["stroke_id"] = 0 if i <= 4 else 1
            row["stroke_end_flag"] = i in (4, 20)
    result = compare_motion_windows(current(source, length=length), experts(length=length))
    key = "length_episodes" if length else "episodes"
    assert len(result[key]) == 1
    episode = result[key][0]
    assert episode["window_count"] == 1
    assert episode["native_end_s"] - episode["native_start_s"] == 5
    if length:
        assert result["windows"][0]["stroke_length_count"] == 1
        assert episode["source_stroke_ids"] == [0]
        assert result["length_review_policy"]["persistence_windows"] == 1
        assert result["length_review_policy"]["min_distinct_strokes"] == 1
    plan = build_session_coaching_plan([], [], {}, None, motion_comparison=result)
    expected = "coaching:short_strokes" if length else "coaching:motion_reference"
    assert [i["feedback_id"] for i in plan["items"]] == [expected]


@pytest.mark.parametrize("length", [False, True])
def test_saved_three_window_v2_policy_can_still_be_replayed_explicitly(length):
    draft_policy = {**DEFAULT_PROTOCOL, "persistence_windows": 3}
    source = stroke_rows(seconds=5) if length else frames(1, seconds=5)
    result = compare_motion_windows(current(source, length=length, protocol=draft_policy),
                                    experts(length=length, protocol=draft_policy))
    key = "length_episodes" if length else "episodes"
    assert result[key] == []


@pytest.mark.parametrize("schema,stride", [("motion_window_protocol_v1", 5), ("motion_window_protocol_v2", 1)])
def test_schema_and_stride_cannot_be_mixed(schema, stride):
    policy = {**DEFAULT_PROTOCOL, "schema": schema, "stride_s": stride}
    with pytest.raises(ValueError):
        current(frames(), protocol=policy)


def test_legacy_grid_remains_readable_but_is_not_compared_to_current_reference():
    old = current(frames(1, seconds=7), protocol=LEGACY_PROTOCOL)
    assert [(w["native_start_s"], w["native_end_s"]) for w in old["windows"]] == [(0, 5), (1, 6), (2, 7)]
    validate_window_build(old)
    with pytest.raises(ValueError, match="protocols"):
        compare_motion_windows(old, experts())
    replay = compare_motion_windows(old, experts(protocol=LEGACY_PROTOCOL))
    assert replay["episodes"][0]["native_end_s"] == 7


@pytest.mark.parametrize("saved_length", [False, True])
def test_verified_legacy_sidecar_is_rebinned_for_new_reference_without_rewrite(tmp_path, saved_length):
    service, source, _, stages = bound_case(tmp_path, seconds=16)
    source = with_strokes(service, source)
    length_reference(service, source, protocol=DEFAULT_PROTOCOL)
    path, saved = save_sidecar(service, source, stages, length=saved_length, protocol=LEGACY_PROTOCOL)
    original = path.read_bytes()
    saved_copy = deepcopy(saved)
    result = service.bootstrap()["motion_comparison"]
    assert result["available"]
    assert result["protocol"] == DEFAULT_PROTOCOL
    assert result["current_source"] == "current_database_reference_grid_after_archive_validation"
    assert [w["native_start_s"] for w in result["windows"]] == [0, 5, 10]
    assert result["length_episodes"] and result["length_episodes"][0]["window_count"] == 3
    assert path.read_bytes() == original
    assert saved == saved_copy
