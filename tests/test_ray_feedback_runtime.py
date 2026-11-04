"""Ray screening availability and unchanged coaching gates through the actual service."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_system.web import FeedbackService  # noqa: E402


def make_service(folder, *, explicit_ray=None, mode="recommendations"):
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "evidence.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript((ROOT / "src/evidence_system/storage/schema.sql").read_text())
        connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            "synthetic:case", "synthetic", "case", "synthetic_performer", "trial", 0, "", "test",
        ))
    return FeedbackService(
        mode=mode, db_path=db, run_id="case", video_path=None,
        video_manifest_path=None, ray_v3_dir=explicit_ray, cohort_benchmark_path=None,
        available_runs=[], display_name="Synthetic recording", interface_version="test",
        study_state_path=folder / "study.json" if mode == "expert-study" else None,
    )


def write_ray(folder, *, run_id="case", tier="high_support_review", eligible=True,
              has_episode=True, candidate_frames=8):
    folder.mkdir(parents=True, exist_ok=True)
    episodes = [{
        "episode_id": "synthetic:episode:1", "t_start_s": 1.0, "t_end_s": 2.0,
        "phase": "antrum", "screening_tier": tier, "source_frame_indices": [1],
        "positive_frame_count": 8, "mean_blocked_contact_fraction": 0.85,
        "duration_s": 1.0, "median_registered_contact_voxel_count": 30,
    }] if has_episode else []
    summary = {
        "schema": "under_ledge_ray_v3_r4_v1", "run_id": run_id,
        "lineage": "adapted_r4_v1", "registration_orientation": "identity",
        "candidate_frame_count": candidate_frames,
        "candidate_episode_count": len(episodes), "evaluable_frame_count": 20,
        "coaching_eligible": eligible,
        "coaching_suppression_reason": None if eligible else "registration_quality_gate",
        "config": {
            "coaching_minimum_episode_duration_s": 0.75,
            "coaching_minimum_positive_frames": 5,
            "coaching_minimum_mean_blocked_fraction": 0.7,
            "coaching_minimum_median_registered_contacts": 20,
            "coaching_minimum_registration_gate_fraction": 0.8,
        },
        "registration_audit": {"contact_gate_pass_frame_fraction": 0.95 if eligible else 0.5},
        "geometry": {"correction_table_sha256": "a" * 64},
        "clock_alignment": {"sha256": "b" * 64},
        "input_artifacts": {"frames": {"sha256": "c" * 64}},
        "user_review_performed": False, "clinician_review_performed": False,
        "claim_boundary": "Automatic screening candidates, not reviewed ground truth.",
    }
    (folder / "ray_v3_summary.json").write_text(json.dumps(summary))
    (folder / "ray_v3_candidate_episodes.json").write_text(json.dumps(episodes))
    (folder / "ray_v3_frame_scores.csv").write_text(
        "frame_idx_global,t_video_s,blocked_contact_fraction,registered_contact_voxel_count\n"
        "1,1.5,0.85,20\n"
    )


def test_missing_analysis_is_not_reported_as_zero_candidates(tmp_path):
    service = make_service(tmp_path / "run")
    payload = service.bootstrap()
    status = payload["detection_status"]["under_ledge"]
    assert status["status"] == "not_configured"
    assert status["available"] is False
    assert status["candidate_episode_count"] is None
    assert "no negative finding" in status["message"]
    assert payload["coaching_plan"]["items"] == []
    assert payload["review_candidates"] == []


def test_generated_bundle_is_discovered_after_service_creation(tmp_path):
    root = tmp_path / "run"
    service = make_service(root)
    assert not service.bootstrap()["detection_status"]["under_ledge"]["available"]
    write_ray(root / "stages/08_under_ledge_ray")
    payload = service.bootstrap()
    status = payload["detection_status"]["under_ledge"]
    assert status["source"] == "generated_sibling" and status["status"] == "completed"
    assert status["coaching_eligible_episode_count"] == 1
    assert status["evaluable_frame_count"] == 20
    item = payload["coaching_plan"]["items"][0]
    assert item["feedback_id"] == "coaching:working_corridor"
    assert item["evidence_status"] == "automatic_geometric_screening_candidate"
    assert item["review_clips"][0]["anchor_t_s"] == 1.5
    provenance = payload["under_ledge_screening"]["provenance"]
    assert provenance["lineage"] == "adapted_r4_v1"
    assert provenance["clock_alignment"]["sha256"] == "b" * 64
    assert provenance["input_artifacts"]["frames"]["sha256"] == "c" * 64
    assert provenance["clinician_review_performed"] is False
    candidate = payload["review_candidates"][0]
    assert candidate["coaching_eligible"] is True
    assert candidate["failed_coaching_gate_ids"] == []
    assert all(gate["passed"] for gate in candidate["coaching_gates"])
    assert candidate["is_ground_truth"] is False


@pytest.mark.parametrize("eligible,tier,has_episode,candidates,expected", [
    (False, "high_support_review", True, 8, "suppressed"),
    (True, "low_support_screen", True, 8, "low_support"),
    (True, "low_support_screen", False, 1, "low_support"),
    (True, "low_support_screen", False, 0, "no_candidate"),
])
def test_availability_does_not_promote_weak_or_suppressed_candidates(
    tmp_path, eligible, tier, has_episode, candidates, expected,
):
    root = tmp_path / "run"
    service = make_service(root)
    write_ray(root / "stages/08_under_ledge_ray", eligible=eligible, tier=tier,
              has_episode=has_episode, candidate_frames=candidates)
    payload = service.bootstrap()
    status = payload["detection_status"]["under_ledge"]
    assert status["available"] and status["status"] == expected
    assert status["coaching_eligible_episode_count"] == 0
    assert payload["coaching_plan"]["items"] == []


def test_wrong_recording_bundle_cannot_generate_feedback(tmp_path):
    root = tmp_path / "run"
    service = make_service(root)
    write_ray(root / "stages/08_under_ledge_ray", run_id="another_recording")
    payload = service.bootstrap()
    status = payload["detection_status"]["under_ledge"]
    assert not status["available"]
    assert "different run" in status["reason"]
    assert payload["coaching_plan"]["items"] == []


def test_no_discovery_outside_database_artifact_root_and_explicit_override(tmp_path):
    other = tmp_path / "other/stages/08_under_ledge_ray"
    write_ray(other)
    service = make_service(tmp_path / "run")
    assert not service.bootstrap()["detection_status"]["under_ledge"]["available"]
    explicit = make_service(tmp_path / "explicit_run", explicit_ray=other)
    status = explicit.bootstrap()["detection_status"]["under_ledge"]
    assert status["available"] and status["source"] == "explicit"


@pytest.mark.parametrize("stale_outputs", [False, True])
def test_not_run_receipt_preserves_geometry_reason(tmp_path, stale_outputs):
    root = tmp_path / "run"
    service = make_service(root)
    folder = root / "stages/08_under_ledge_ray"
    folder.mkdir(parents=True)
    if stale_outputs:
        write_ray(folder)
    (folder / "ray_v3_status.json").write_text(json.dumps({
        "schema": "ray_status_v1", "run_id": "case", "status": "not_run",
        "reason": "validated_signal_geometry_not_configured",
        "message": "Under-ledge analysis was not run: calibrated geometry is required.",
    }))
    status = service.bootstrap()["detection_status"]["under_ledge"]
    assert status["status"] == "not_configured" and not status["available"]
    assert status["reason"] == "validated_signal_geometry_not_configured"
    assert "was not run" in status["message"]
    assert status["candidate_episode_count"] is None


def test_zero_evaluable_contacts_is_not_a_negative_finding(tmp_path):
    root = tmp_path / "run"
    service = make_service(root)
    folder = root / "stages/08_under_ledge_ray"
    write_ray(folder, has_episode=False, candidate_frames=0)
    path = folder / "ray_v3_summary.json"
    summary = json.loads(path.read_text())
    summary.update(evaluable_frame_count=0, frames_with_raw_contact=0,
                   frames_passing_registration_gate=0)
    path.write_text(json.dumps(summary))
    payload = service.bootstrap()
    status = payload["detection_status"]["under_ledge"]
    assert status["status"] == "not_evaluable"
    assert "could not assess" in status["message"]
    assert payload["coaching_plan"]["items"] == []


def test_completed_receipt_without_outputs_remains_unavailable(tmp_path):
    root = tmp_path / "run"
    service = make_service(root)
    folder = root / "stages/08_under_ledge_ray"
    folder.mkdir(parents=True)
    (folder / "ray_v3_status.json").write_text(json.dumps({
        "run_id": "case", "status": "completed",
    }))
    status = service.bootstrap()["detection_status"]["under_ledge"]
    assert not status["available"] and status["candidate_episode_count"] is None


@pytest.mark.parametrize("mode", ["recommendations", "expert-study"])
def test_brief_candidate_is_returned_separately_without_generating_coaching(tmp_path, mode):
    root = tmp_path / "run"
    service = make_service(root, mode=mode)
    folder = root / "stages/08_under_ledge_ray"
    write_ray(folder, tier="low_support_screen")
    path = folder / "ray_v3_candidate_episodes.json"
    episodes = json.loads(path.read_text())
    episodes[0].update(t_start_s=21.0, t_end_s=21 + 1 / 3, duration_s=1 / 3,
                       positive_frame_count=6, mean_blocked_contact_fraction=0.8666666667,
                       median_registered_contact_voxel_count=192)
    path.write_text(json.dumps(episodes))
    if mode == "expert-study":
        service.start_study("synthetic_reviewer")
        payload = service.analyze_study("synthetic_reviewer")
        assert payload["feedback"] == []
        assert payload["expert_proposed_moments"] == []
    else:
        payload = service.bootstrap()
        assert payload["coaching_plan"]["items"] == []
    candidate, = payload["review_candidates"]
    assert candidate["review_status"] == "review_only"
    assert candidate["coaching_eligible"] is False
    assert candidate["is_ground_truth"] is False
    assert candidate["failed_coaching_gate_ids"] == ["duration"]
    assert candidate["unknown_coaching_gate_ids"] == []
    assert "0.33 s is below 0.75 s" in candidate["filter_reasons"][0]
    assert candidate["t_start_s"] == 21
    assert candidate["t_end_s"] == pytest.approx(21 + 1 / 3)
    assert "not continuous" in candidate["interval_interpretation"]


@pytest.mark.parametrize("field,value,gate_id", [
    ("positive_frame_count", 3, "positive_frames"),
    ("mean_blocked_contact_fraction", 0.58, "mean_blocked_fraction"),
    ("median_registered_contact_voxel_count", 10, "registered_contacts"),
])
def test_each_saved_episode_threshold_has_a_structured_filter_reason(tmp_path, field, value, gate_id):
    root = tmp_path / "run"
    service = make_service(root)
    folder = root / "stages/08_under_ledge_ray"
    write_ray(folder, tier="low_support_screen")
    path = folder / "ray_v3_candidate_episodes.json"
    episodes = json.loads(path.read_text())
    episodes[0][field] = value
    path.write_text(json.dumps(episodes))
    payload = service.bootstrap()
    candidate, = payload["review_candidates"]
    assert candidate["failed_coaching_gate_ids"] == [gate_id]
    assert len(candidate["filter_reasons"]) == 1
    assert payload["coaching_plan"]["items"] == []


def test_suppressed_high_support_candidate_keeps_run_gate_reason(tmp_path):
    root = tmp_path / "run"
    service = make_service(root)
    write_ray(root / "stages/08_under_ledge_ray", eligible=False)
    payload = service.bootstrap()
    candidate, = payload["review_candidates"]
    assert candidate["screening_tier"] == "high_support_review"
    assert candidate["failed_coaching_gate_ids"] == ["run_registration"]
    gate = candidate["coaching_gates"][-1]
    assert gate["reason"] == "registration_quality_gate"
    assert gate["observed_value"] == 0.5 and gate["threshold"] == 0.8
    assert payload["coaching_plan"]["items"] == []


def test_absent_historical_metric_and_thresholds_remain_unknown(tmp_path):
    root = tmp_path / "run"
    service = make_service(root)
    folder = root / "stages/08_under_ledge_ray"
    write_ray(folder, tier="low_support_screen")
    path = folder / "ray_v3_summary.json"
    summary = json.loads(path.read_text())
    del summary["config"]
    path.write_text(json.dumps(summary))
    payload = service.bootstrap()
    assert payload["detection_status"]["under_ledge"]["before_150_s_candidate_frame_count"] is None
    candidate, = payload["review_candidates"]
    assert len(candidate["unknown_coaching_gate_ids"]) == 4
    assert candidate["failed_coaching_gate_ids"] == []
    assert payload["coaching_plan"]["items"] == []
