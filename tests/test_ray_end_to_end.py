"""Actual synthetic geometry through Ray-v3, persisted review and coaching.

No detector output is mocked. A known intervening wall is either present or
removed before drilling; each contact voxel occurs only once in the source.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_system.coaching_plan import build_session_coaching_plan  # noqa: E402
from evidence_system.frame_pipeline.phase_geometry import (  # noqa: E402
    SYNTHETIC_PROVENANCE,
    resolve_phase_geometry,
)
from evidence_system.ray_v3_view import build_ray_v3_view  # noqa: E402
from evidence_system.registry import RunRecord  # noqa: E402
from evidence_system.source_config import ResolvedRunSources  # noqa: E402
from evidence_system.under_ledge_ray import (  # noqa: E402
    build_under_ledge_ray,
    write_ray_build,
)


@pytest.fixture
def synthetic_case(tmp_path, monkeypatch):
    record = RunRecord(
        dataset_id="synthetic",
        run_id="ray_case",
        participant_id="generated",
        trial_id="generated",
        training_year="",
        exp_id="",
        anatomy_key="fixture",
        raw_anatomy_token="fixture",
        anatomy_mapping_status="synthetic",
        anatomy_mapping_basis="deterministically generated test geometry",
        raw_run_relpath="raw",
        edt_profile="synthetic_grid",
        phase_annotation_id="",
        source_status="generated",
        external_case_id="",
        golden_reference_path=tmp_path / "unused",
    )
    profile = {
        "edt_profile": record.edt_profile,
        "meters_per_simulator_unit": 0.1,
        "edt_distance_scale_mm": 0.25,
        "volume_dimensions_sim": [3.2, 3.2, 3.2],
        "provenance": SYNTHETIC_PROVENANCE,
        "phase_signal_geometry": {
            "schema": "synthetic_signal_geometry_v1",
            "global_run_id": record.global_run_id,
            "edt_profile": record.edt_profile,
            "actual_specimen": "generated",
            "calibration_recording_id": "generated",
            "rotation_source": "synthetic identity",
            "volume_quaternion_xyzw": [0, 0, 0, 1],
            "recorder_scale_m_per_simulator_unit": 0.1,
            "edt_distance_scale_mm": 0.25,
            "volume_dimensions_sim": [3.2, 3.2, 3.2],
            "geometry_quality": "synthetic",
            "grid_resolution_xyz": [32, 32, 32],
        },
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "anatomy_profiles.json"
    config_path.write_text(json.dumps({"profiles": {record.anatomy_key: profile}}))
    monkeypatch.setenv("MASTOID_ANATOMY_PROFILES", str(config_path))

    contacts = [(24, y, z) for y in range(11, 21) for z in range(11, 21)]
    wall = [(8, y, z) for y in range(32) for z in range(32)]
    # Ray-v3 consumes only the sign of Bone.edt: negative marks initial bone.
    grid = np.ones((32, 32, 32), dtype=np.float32)
    for x, y, z in contacts + wall:
        grid[z, y, x] = -1.0
    edt_dir = tmp_path / "edt"
    edt_dir.mkdir()
    with (edt_dir / "Bone.edt").open("wb") as stream:
        stream.write(b"G3 1 FLOAT 32 32 32\n")
        stream.write((" ".join(str(value) for value in np.eye(4).ravel()) + "\n").encode("ascii"))
        stream.write(grid.tobytes(order="C"))
    sources = ResolvedRunSources(
        raw_run_dir=tmp_path / "raw",
        edt_dir=edt_dir,
        phase_annotation_file=None,
        golden_reference_path=tmp_path / "unused",
    )
    geometry = resolve_phase_geometry(
        profile,
        global_run_id=record.global_run_id,
        edt_profile=record.edt_profile,
    )
    return tmp_path, record, sources, geometry, contacts, wall


def _execute(case, name, *, wall_removed=False, shift=0.0, no_contacts=False):
    root, record, sources, geometry, contacts, wall = case
    frame_times = [1.0, 2.0, 2.25, 2.5, 2.75, 3.0]
    frames: list[dict[str, object]] = [
        {
            "global_run_id": record.global_run_id,
            "frame_idx_global": index,
            "frame_evidence_id": f"{record.global_run_id}:frame:{index}",
            "t_video_s": time + shift,
            "burr_mm": 2.0,
            "idx_Bone_x": 24,
            "idx_Bone_y": 16,
            "idx_Bone_z": 16,
            # In the fixed geometry this is grid(-4,16,16), before the wall at x=8.
            "camera_x": -0.2,
            "camera_y": 0.0,
            "camera_z": 0.0,
            "volume_x": 0.0,
            "volume_y": 0.0,
            "volume_z": 0.0,
            "phase": "antrum",
        }
        for index, time in enumerate(frame_times)
    ]
    removals: list[dict[str, object]] = []

    def add_removal(xyz, frame_index, time):
        x, y, z = xyz
        removals.append(
            {
                "global_run_id": record.global_run_id,
                "removed_voxel_id": f"{record.global_run_id}:voxel:{len(removals)}",
                "x": x,
                "y": 31 - y,
                "z": z,
                "edt_x": x,
                "edt_y": y,
                "edt_z": z,
                "frame_idx_global": frame_index,
                "t_video_s": time + shift,
            }
        )

    if not no_contacts:
        if wall_removed:
            for xyz in wall:
                add_removal(xyz, 0, 0.5)
        for index in range(1, 6):
            for xyz in contacts[(index - 1) * 20 : index * 20]:
                add_removal(xyz, index, frame_times[index])
    assert len({(row["x"], row["y"], row["z"]) for row in removals}) == len(removals)

    build = build_under_ledge_ray(
        frames,
        removals,
        record,
        sources,
        project_root=root,
        geometry_profile=geometry,
    )
    output = root / name
    write_ray_build(output, build)
    view = build_ray_v3_view(record.run_id, output)
    plan = build_session_coaching_plan([], [], view)
    return build, view, plan


def test_actual_wall_generates_review_and_earlier_removal_clears_it(synthetic_case):
    blocked, blocked_view, blocked_plan = _execute(synthetic_case, "blocked")
    assert blocked.manifest["candidate_frame_count"] == 5
    assert blocked.manifest["evaluable_frame_count"] == 5
    assert blocked.manifest["coaching_eligible"] is True
    assert [row["registered_contact_voxel_count"] for row in blocked.rows[1:]] == [20] * 5
    assert [row["blocked_contact_fraction"] for row in blocked.rows[1:]] == [1.0] * 5
    assert len(blocked.episodes) == 1
    episode = blocked.episodes[0]
    assert episode["screening_tier"] == "high_support_review"
    assert episode["positive_frame_count"] == 5
    assert episode["source_frame_indices"] == [1, 2, 3, 4, 5]
    assert (episode["t_start_s"], episode["t_end_s"]) == (2.0, 3.0)
    assert blocked_view["detection_status"]["status"] == "completed"
    assert len(blocked_plan["items"]) == 1
    item = blocked_plan["items"][0]
    assert item["feedback_id"] == "coaching:working_corridor"
    assert item["source_ids"] == [episode["episode_id"]]
    assert item["evidence_status"] == "automatic_geometric_screening_candidate"
    assert item["review_clips"][0]["source_episode_ids"] == [episode["episode_id"]]
    assert (item["review_clips"][0]["t_start_s"], item["review_clips"][0]["t_end_s"]) == (2.0, 3.0)
    assert blocked_view["provenance"]["geometry"]["global_run_id"] == "synthetic:ray_case"
    assert blocked_view["provenance"]["clinician_review_performed"] is False

    clear, clear_view, clear_plan = _execute(synthetic_case, "removed_wall", wall_removed=True)
    # The earlier wall removals add one unregistered contact-proxy frame. The
    # original run gate still passes, so geometry alone explains the lost card.
    assert clear.manifest["registration_audit"][
        "contact_gate_pass_frame_fraction"
    ] == pytest.approx(5 / 6)
    assert clear.manifest["coaching_eligible"] is True
    assert clear.manifest["evaluable_frame_count"] == 5
    assert [row["registered_contact_voxel_count"] for row in clear.rows[1:]] == [20] * 5
    assert [row["blocked_contact_fraction"] for row in clear.rows[1:]] == [0.0] * 5
    assert clear.manifest["candidate_frame_count"] == 0
    assert clear.episodes == []
    assert clear_view["detection_status"]["status"] == "no_candidate"
    assert clear_plan["items"] == []


@pytest.mark.parametrize("wall_removed", [False, True])
def test_translating_all_clocks_preserves_geometry_and_shifts_review_bounds(
    synthetic_case, wall_removed
):
    original, original_view, original_plan = _execute(
        synthetic_case,
        "original",
        wall_removed=wall_removed,
    )
    shifted, shifted_view, shifted_plan = _execute(
        synthetic_case,
        "shifted",
        wall_removed=wall_removed,
        shift=137.0,
    )
    for before, after in zip(original.rows, shifted.rows, strict=True):
        assert after["t_video_s"] == before["t_video_s"] + 137.0
        for key in (
            "ray_state",
            "registered_contact_voxel_count",
            "sampled_contact_count",
            "blocked_contact_fraction",
            "blocker_voxel_count_sum",
        ):
            assert after[key] == before[key]
    assert shifted_view["detection_status"] == original_view["detection_status"]
    assert len(shifted_plan["items"]) == len(original_plan["items"])
    for before, after in zip(original_plan["items"], shifted_plan["items"], strict=True):
        assert after["source_ids"] == before["source_ids"]
        for key in ("t_start_s", "t_end_s"):
            assert after[key] == before[key] + 137.0
            assert after["review_clips"][0][key] == before["review_clips"][0][key] + 137.0
        assert (
            after["review_clips"][0]["anchor_t_s"]
            == before["review_clips"][0]["anchor_t_s"] + 137.0
        )


def test_no_removed_contact_evidence_is_not_evaluable_and_cannot_coach(synthetic_case):
    build, view, plan = _execute(synthetic_case, "no_contacts", no_contacts=True)
    assert build.manifest["evaluable_frame_count"] == 0
    assert all(row["ray_state"] == "no_contact_evidence" for row in build.rows)
    assert all(row["blocked_contact_fraction"] is None for row in build.rows)
    assert view["detection_status"]["status"] == "not_evaluable"
    assert "could not assess" in view["detection_status"]["message"]
    assert view["coaching_episodes"] == []
    assert plan["items"] == []
