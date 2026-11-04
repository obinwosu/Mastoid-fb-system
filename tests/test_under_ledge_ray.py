"""Synthetic occlusion, chronology, registration and integration contracts."""

from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import numpy as np  # noqa: E402

from evidence_system.coaching_plan import build_session_coaching_plan  # noqa: E402
from evidence_system.frame_pipeline.phase_geometry import (  # noqa: E402
    SYNTHETIC_PROVENANCE,
    resolve_phase_geometry,
)
from evidence_system.ray_v3_view import build_ray_v3_view  # noqa: E402
from evidence_system.under_ledge_ray import (  # noqa: E402
    COACHING_POLICY_VERSION,
    RayBuild,
    RayConfig,
    build_removal_index,
    build_under_ledge_ray,
    detect_frame_rows,
    group_episodes,
    ray_residual_bone_evidence,
    world_to_grid,
    write_ray_build,
)


def fixture_profile():
    return {
        "edt_profile": "synthetic",
        "meters_per_simulator_unit": 0.1,
        "edt_distance_scale_mm": 1.0,
        "volume_dimensions_sim": [1.2] * 3,
        "provenance": SYNTHETIC_PROVENANCE,
        "phase_signal_geometry": {
            "schema": "synthetic_signal_geometry_v1",
            "global_run_id": "DEMO:CASE",
            "edt_profile": "synthetic",
            "actual_specimen": "generated",
            "calibration_recording_id": "fixture",
            "rotation_source": "generated",
            "volume_quaternion_xyzw": [0, 0, 0, 1],
            "recorder_scale_m_per_simulator_unit": 0.1,
            "edt_distance_scale_mm": 1.0,
            "volume_dimensions_sim": [1.2] * 3,
            "geometry_quality": "synthetic",
            "grid_resolution_xyz": [12] * 3,
        },
    }


def geometry():
    return resolve_phase_geometry(
        fixture_profile(), global_run_id="DEMO:CASE", edt_profile="synthetic"
    )


def frame(index=0, time=1.0):
    return {
        "frame_idx_global": index,
        "t_video_s": time,
        "burr_mm": 2.0,
        "idx_Bone_x": 9,
        "idx_Bone_y": 6,
        "idx_Bone_z": 6,
        "camera_x": -0.09,
        "camera_y": 0.0,
        "camera_z": 0.0,
        "volume_x": 0.0,
        "volume_y": 0.0,
        "volume_z": 0.0,
        "volume_qx": 1.0,
        "volume_qy": 0.0,
        "volume_qz": 0.0,
        "volume_qw": 0.0,
        "phase": "antrum",
    }


def voxel(index=0, time=1.0, xyz=(9, 6, 6)):
    x, y, z = xyz
    return {
        "x": x,
        "y": 11 - y,
        "z": z,
        "edt_x": x,
        "edt_y": y,
        "edt_z": z,
        "frame_idx_global": index,
        "t_video_s": time,
    }


class RayGeometryTests(unittest.TestCase):
    def setUp(self):
        self.bone = np.zeros((12, 12, 12), dtype=bool)
        self.earliest = np.full(self.bone.shape, np.inf)

    def ray(self, time=1.0, radius=1.0, camera=(-3, 6, 6)):
        return ray_residual_bone_evidence(
            camera,
            (9, 6, 6),
            time_s=time,
            burr_radius_mm=radius,
            initial_bone_zyx=self.bone,
            earliest_removal_s_zyx=self.earliest,
            voxel_spacing_xyz_mm=[1.0] * 3,
        )

    def test_initial_bone_blocks_but_empty_grid_is_clear(self):
        self.assertFalse(self.ray()["blocked"])
        self.bone[6, 6, 4] = True
        self.assertTrue(self.ray()["blocked"])

    def test_removal_after_frame_blocks_equal_or_before_does_not(self):
        self.bone[6, 6, 4] = True
        self.earliest[6, 6, 4] = 0.1
        self.assertTrue(self.ray(time=0.1 - 1e-10)["blocked"])
        self.assertFalse(self.ray(time=0.1)["blocked"])
        self.assertFalse(self.ray(time=0.1 + 1e-10)["blocked"])

    def test_terminal_burr_sphere_excludes_equal_radius(self):
        self.bone[6, 6, 8] = True
        self.assertFalse(self.ray(radius=1.0)["blocked"])
        self.bone[6, 6, 7] = True
        self.assertTrue(self.ray(radius=1.0)["blocked"])

    def test_far_outside_camera_clips_without_huge_sampling(self):
        self.bone[6, 6, 4] = True
        self.assertTrue(self.ray(camera=(-1e9, 6, 6))["blocked"])
        self.assertFalse(self.ray(camera=(float("nan"), 6, 6))["evaluable"])

    def test_off_axis_ray_uses_integer_voxel_centers(self):
        def check():
            return ray_residual_bone_evidence(
                (-3, 1, 6),
                (9, 8, 6),
                time_s=1,
                burr_radius_mm=1,
                initial_bone_zyx=self.bone,
                earliest_removal_s_zyx=self.earliest,
                voxel_spacing_xyz_mm=[1.0] * 3,
            )["blocked"]

        self.bone[6, 5, 4] = True
        self.assertTrue(check())
        self.bone[6, 5, 4] = False
        self.bone[6, 6, 4] = True
        self.assertFalse(check())

    def test_world_projection_uses_fixed_quaternion_and_recorded_translation(self):
        calibrated = geometry()
        calibrated["volume_quaternion_xyzw"] = [0, 0, math.sqrt(0.5), math.sqrt(0.5)]
        # Local +x3 grid voxels becomes world +y .03 m after rotation.
        np.testing.assert_allclose(
            world_to_grid([2, 3.03, 4], [2, 3, 4], calibrated, (12, 12, 12)),
            [9, 6, 6],
            atol=1e-12,
        )


class RemovalAndStateTests(unittest.TestCase):
    def test_native_mapping_and_exact_float64_removal_boundary(self):
        contacts, earliest, counts = build_removal_index([voxel(time=0.1)], (12, 12, 12), {0: 0.1})
        self.assertEqual(contacts[0], [(9, 6, 6)])
        self.assertEqual(earliest[6, 6, 9], 0.1)
        self.assertEqual(earliest.dtype, np.float64)
        self.assertEqual(counts["removed_voxel_rows_in_bounds"], 1)

    def test_mismatched_geometry_and_event_clock_fail(self):
        bad = voxel()
        bad["edt_y"] = 5
        with self.assertRaisesRegex(ValueError, "coordinates disagree"):
            build_removal_index([bad], (12, 12, 12), {0: 1.0})
        with self.assertRaisesRegex(ValueError, "mapped clock"):
            build_removal_index([voxel(time=2)], (12, 12, 12), {0: 1.0})
        with self.assertRaisesRegex(ValueError, "mapped clock"):
            build_removal_index([voxel(time=1 + 1e-10)], (12, 12, 12), {0: 1.0})
        with self.assertRaisesRegex(ValueError, "mapped clock"):
            build_removal_index([voxel(index=1, time=1)], (12, 12, 12), {0: 1.0, 1: 2.0})
        bad = voxel()
        bad["x"] = 0.5
        with self.assertRaisesRegex(ValueError, "integer native"):
            build_removal_index([bad], (12, 12, 12), {0: 1.0})

    def test_outside_native_voxels_are_not_contact_targets(self):
        outside = voxel(xyz=(15, 6, 6))
        contacts, _, counts = build_removal_index([outside], (12, 12, 12), {0: 1.0})
        self.assertEqual(contacts, {})
        self.assertEqual(counts["removed_voxel_rows_out_of_bounds"], 1)

    def test_duplicate_rows_do_not_inflate_contact_support(self):
        contacts, _, counts = build_removal_index([voxel(), voxel()], (12, 12, 12), {0: 1.0})
        self.assertEqual(len(contacts[0]), 1)
        self.assertEqual(counts["duplicate_frame_contact_rows_ignored"], 1)

    def states(self, frames, contacts, bone):
        return detect_frame_rows(
            frames,
            contacts,
            initial_bone_zyx=bone,
            earliest_removal_s_zyx=np.full(bone.shape, np.inf),
            geometry=geometry(),
            resolution_xyz=(12, 12, 12),
            config=RayConfig(),
        )

    def test_zero_tip_index_is_valid_and_missing_is_unknown(self):
        row = frame()
        row["idx_Bone_x"] = 0
        bone = np.zeros((12, 12, 12), bool)
        bone[6, 6, 0] = True
        result = self.states([row], {0: [(0, 6, 6)]}, bone)[0]
        self.assertEqual(result["registered_contact_voxel_count"], 1)
        self.assertEqual(result["ray_state"], "clear_line_of_sight_at_threshold")
        row["idx_Bone_x"] = None
        result = self.states([row], {0: [(0, 6, 6)]}, bone)[0]
        self.assertEqual(result["ray_state"], "not_evaluable_tip_grid_missing")
        self.assertIsNone(result["blocked_contact_fraction"])

    def test_registration_failure_and_missing_camera_are_not_clear(self):
        bone = np.ones((12, 12, 12), bool)
        result = self.states([frame()], {0: [(0, 6, 6)]}, bone)[0]
        self.assertEqual(result["ray_state"], "not_evaluable_contact_registration_failed")
        row = frame()
        row["camera_x"] = None
        result = self.states([row], {0: [(9, 6, 6)]}, bone)[0]
        self.assertEqual(result["ray_state"], "not_evaluable_camera_pose_missing")
        self.assertIsNone(result["blocked_contact_fraction"])


class GroupAndBuildTests(unittest.TestCase):
    def positive(self, index, time, contacts=20, fraction=0.7):
        return {
            "frame_idx_global": index,
            "t_video_s": time,
            "phase": "antrum",
            "ray_state": "contact_occlusion_candidate",
            "registered_contact_voxel_count": contacts,
            "blocked_contact_fraction": fraction,
        }

    def test_non_duration_high_support_thresholds_and_gap_are_unchanged(self):
        rows = [self.positive(i, i * 0.25) for i in range(5)]
        result = group_episodes(rows, RayConfig())
        self.assertEqual(result[0]["screening_tier"], "high_support_review")
        low = copy.deepcopy(rows)
        low[0]["registered_contact_voxel_count"] = 19
        low[1]["registered_contact_voxel_count"] = 19
        low[2]["registered_contact_voxel_count"] = 19
        self.assertEqual(
            group_episodes(low, RayConfig())[0]["screening_tier"], "low_support_screen"
        )
        at_gap = group_episodes([self.positive(0, 0), self.positive(1, 1)], RayConfig())
        self.assertEqual(at_gap[0]["positive_frame_count"], 2)
        beyond_gap = group_episodes([self.positive(0, 0), self.positive(1, 1.00001)], RayConfig())
        self.assertEqual(beyond_gap, [])

    def test_user_duration_boundary_is_stable_at_shifted_recording_times(self):
        for start in (0.0, 100.0, 7200.0):
            for span, expected in (
                (0.299, "low_support_screen"),
                (0.3, "high_support_review"),
                (0.333, "high_support_review"),
            ):
                with self.subTest(start=start, span=span):
                    rows = [self.positive(i, start + span * i / 5, fraction=0.85) for i in range(6)]
                    (episode,) = group_episodes(rows, RayConfig())
                    self.assertEqual(episode["screening_tier"], expected)
                    self.assertAlmostEqual(episode["duration_s"], span, places=11)
                    self.assertEqual(episode["t_start_s"], rows[0]["t_video_s"])
                    self.assertEqual(episode["t_end_s"], rows[-1]["t_video_s"])

    def test_short_policy_does_not_waive_other_coaching_gates(self):
        for count, blocked, contacts in ((4, 0.85, 20), (6, 0.699, 20), (6, 0.85, 19)):
            with self.subTest(count=count, blocked=blocked, contacts=contacts):
                rows = [
                    self.positive(i, 0.333 * i / (count - 1), contacts, blocked)
                    for i in range(count)
                ]
                (episode,) = group_episodes(rows, RayConfig())
                self.assertEqual(episode["screening_tier"], "low_support_screen")

    def test_saved_old_and_new_policies_remain_independent_in_view_and_coaching(self):
        rows = [self.positive(i, 100 + 0.333 * i / 5, fraction=0.85) for i in range(6)]
        old_config = RayConfig(coaching_minimum_episode_duration_s=0.75)
        new_config = RayConfig()
        old_values, new_values = asdict(old_config), asdict(new_config)
        self.assertEqual(
            [key for key in old_values if old_values[key] != new_values[key]],
            ["coaching_minimum_episode_duration_s"],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, config in (("old", old_config), ("new", new_config)):
                manifest = {
                    "run_id": "CASE",
                    "config": asdict(config),
                    "coaching_eligible": True,
                    "evaluable_frame_count": len(rows),
                    "registration_audit": {"contact_gate_pass_frame_fraction": 1.0},
                }
                build = RayBuild(rows, group_episodes(rows, config), manifest)
                write_ray_build(root / label, build)
                if label == "old":
                    preserved = {path.name: path.read_bytes() for path in (root / "old").iterdir()}
            for label, expected_count, threshold in (("old", 0, 0.75), ("new", 1, 0.3)):
                view = build_ray_v3_view("CASE", root / label)
                plan = build_session_coaching_plan([], [], view)
                self.assertEqual(len(plan["items"]), expected_count)
                (candidate,) = view["review_candidates"]
                gate = next(
                    row for row in candidate["coaching_gates"] if row["gate_id"] == "duration"
                )
                self.assertEqual(gate["threshold"], threshold)
                self.assertEqual(gate["passed"], bool(expected_count))
                self.assertFalse(candidate["is_ground_truth"])
            self.assertEqual(
                {path.name: path.read_bytes() for path in (root / "old").iterdir()},
                preserved,
            )

    def build(self, frames, voxels, supplied_geometry=None):
        grid = np.ones((12, 12, 12), np.float32)
        grid[6, 6, 9] = -1
        grid[6, 6, 4] = -1
        record = SimpleNamespace(
            run_id="CASE", global_run_id="DEMO:CASE", anatomy_key="fixture", edt_profile="synthetic"
        )
        with (
            patch(
                "evidence_system.under_ledge_ray.load_anatomy_profile",
                return_value=fixture_profile(),
            ),
            patch(
                "evidence_system.under_ledge_ray.read_edt_file",
                return_value=(grid, (12, 12, 12)),
            ),
        ):
            return build_under_ledge_ray(
                frames,
                voxels,
                record,
                SimpleNamespace(edt_dir=Path("unused")),
                project_root=Path("."),
                geometry_profile=supplied_geometry,
            )

    def test_build_preserves_inputs_writes_provenance_and_run_gate(self):
        frames = [frame(i, 1 + i * 0.25) for i in range(5)]
        frames[-1]["idx_Bone_x"] = None
        voxels = [voxel(i, 1 + i * 0.25) for i in range(5)]
        original = copy.deepcopy((frames, voxels))
        build = self.build(frames, voxels, geometry())
        self.assertEqual((frames, voxels), original)
        self.assertEqual(
            build.manifest["registration_audit"]["contact_gate_pass_frame_fraction"], 0.8
        )
        self.assertTrue(build.manifest["coaching_eligible"])
        self.assertFalse(build.manifest["user_review_performed"])
        self.assertEqual(build.manifest["lineage"], "adapted_r4_v1")
        self.assertEqual(build.manifest["config"]["coaching_minimum_episode_duration_s"], 0.3)
        self.assertEqual(build.manifest["coaching_policy"]["version"], COACHING_POLICY_VERSION)
        self.assertEqual(build.manifest["coaching_policy"]["change_date"], "2026-11-03")
        self.assertEqual(
            build.manifest["coaching_policy"]["change_source"], "explicit_user_instruction"
        )
        self.assertEqual(build.manifest["coaching_policy"]["previous_value_s"], 0.75)
        frames[-2]["idx_Bone_x"] = None
        failed = self.build(frames, voxels)
        self.assertFalse(failed.manifest["coaching_eligible"])
        with tempfile.TemporaryDirectory() as directory:
            write_ray_build(Path(directory), build)
            summary = json.loads((Path(directory) / "ray_v3_summary.json").read_text())
            self.assertEqual(summary["candidate_episode_count"], len(build.episodes))
            self.assertTrue((Path(directory) / "ray_v3_frame_scores.csv").is_file())

    def test_stale_geometry_contract_is_rejected(self):
        stale = geometry()
        stale["edt_distance_scale_mm"] = 2
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.build([frame()], [voxel()], stale)

    def test_no_contact_is_not_registration_failure(self):
        build = self.build([frame()], [])
        self.assertEqual(build.manifest["coaching_suppression_reason"], "no_contact_evidence")
        self.assertEqual(build.rows[0]["ray_state"], "no_contact_evidence")
        self.assertIsNone(build.rows[0]["blocked_contact_fraction"])

    def test_explicit_foreign_run_is_rejected(self):
        row = frame()
        row["global_run_id"] = "DEMO:OTHER"
        with self.assertRaisesRegex(ValueError, "different run"):
            self.build([row], [voxel()])


if __name__ == "__main__":
    unittest.main()
