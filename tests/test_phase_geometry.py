"""Synthetic checks for explicit phase geometry and unchanged legacy behavior."""

from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import numpy as np  # noqa: E402

from evidence_system.frame_pipeline.edt_enrichment import EdtFrameBuild  # noqa: E402
from evidence_system.frame_pipeline.phase_geometry import (  # noqa: E402
    R4_RECORDER_SCALE,
    R4_SCHEMA,
    SYNTHETIC_PROVENANCE,
    SYNTHETIC_SCHEMA,
    resolve_phase_geometry,
)
from evidence_system.frame_pipeline.tip_edt import enrich_tip_edt  # noqa: E402


def synthetic_profile() -> dict:
    """Use a nonidentity calibration and a deliberately different recorded pose."""
    return {
        "edt_profile": "fixture_grid",
        "meters_per_simulator_unit": 0.05,
        "edt_distance_scale_mm": 0.2,
        "volume_dimensions_sim": [1.0, 1.0, 1.0],
        "provenance": SYNTHETIC_PROVENANCE,
        "phase_signal_geometry": {
            "schema": SYNTHETIC_SCHEMA,
            "global_run_id": "SYNTHETIC:CASE",
            "edt_profile": "fixture_grid",
            "actual_specimen": "generated",
            "calibration_recording_id": "generated_case",
            "rotation_source": "generated fixed 90 degree z rotation",
            "volume_quaternion_xyzw": [0, 0, math.sqrt(0.5), math.sqrt(0.5)],
            "recorder_scale_m_per_simulator_unit": 0.05,
            "edt_distance_scale_mm": 0.2,
            "volume_dimensions_sim": [1.0, 1.0, 1.0],
            "geometry_quality": "synthetic",
        },
    }


def resolve(profile: dict):
    return resolve_phase_geometry(
        profile, global_run_id="SYNTHETIC:CASE", edt_profile="fixture_grid"
    )


class GeometryContractTests(unittest.TestCase):
    def test_absent_optional_block_preserves_manual_contract(self) -> None:
        self.assertIsNone(resolve({}))

    def test_fixture_validation_does_not_mutate_profile_or_invent_review(self) -> None:
        profile = synthetic_profile()
        original = copy.deepcopy(profile)
        result = resolve(profile)
        self.assertEqual(profile, original)
        self.assertEqual(result["schema"], SYNTHETIC_SCHEMA)
        self.assertFalse(result["user_review_performed"])
        self.assertFalse(result["clinician_review_performed"])
        self.assertIn("source metadata/voxel_volume", result["removal_volume_definition"])
        self.assertIn("no burr subtraction", result["removed_distance_definition"])

    def test_rejects_wrong_run_edt_or_synthetic_provenance(self) -> None:
        for field, value in (
            ("global_run_id", "SYNTHETIC:OTHER"),
            ("edt_profile", "wrong_grid"),
            ("schema", "unversioned"),
        ):
            profile = synthetic_profile()
            profile["phase_signal_geometry"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                resolve(profile)
        profile = synthetic_profile()
        profile["provenance"] = "ordinary legacy profile"
        with self.assertRaisesRegex(ValueError, "generated fixture"):
            resolve(profile)

    def test_rejects_inconsistent_profile_scales_dimensions(self) -> None:
        for field, value in (
            ("meters_per_simulator_unit", 0.02),
            ("edt_distance_scale_mm", 0.4),
            ("volume_dimensions_sim", [1.1, 1, 1]),
        ):
            profile = synthetic_profile()
            profile[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                resolve(profile)

    def test_rejects_bad_numeric_values_and_quaternions(self) -> None:
        for field, value in (
            ("recorder_scale_m_per_simulator_unit", float("nan")),
            ("edt_distance_scale_mm", 0),
            ("volume_dimensions_sim", [1, -1, 1]),
            ("volume_quaternion_xyzw", [0, 0, 0, 0]),
            ("volume_quaternion_xyzw", [0, 0, float("inf"), 1]),
            ("volume_quaternion_xyzw", [0, 0, 0, 2]),
        ):
            profile = synthetic_profile()
            profile["phase_signal_geometry"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                resolve(profile)

    def test_real_contract_preserves_needs_review_and_requires_source_hash(self) -> None:
        profile = synthetic_profile()
        profile["meters_per_simulator_unit"] = R4_RECORDER_SCALE
        contract = profile["phase_signal_geometry"]
        contract.update(
            schema=R4_SCHEMA,
            recorder_scale_m_per_simulator_unit=R4_RECORDER_SCALE,
            correction_table_sha256="a" * 64,
            geometry_quality="needs_review",
        )
        self.assertEqual(resolve(profile)["geometry_quality"], "needs_review")
        for value in ("", "missing", "x" * 64):
            contract["correction_table_sha256"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve(profile)
        contract["correction_table_sha256"] = "a" * 64
        contract["recorder_scale_m_per_simulator_unit"] = 0.02
        profile["meters_per_simulator_unit"] = 0.02
        with self.assertRaisesRegex(ValueError, "common recorder scale"):
            resolve(profile)


class TipGeometryIntegrationTests(unittest.TestCase):
    def build(self, profile: dict):
        # World translation (2,3,4) must be subtracted. The calibrated rotation
        # maps local +x to world +y; the recorded identity is intentionally wrong.
        frames = [{
            "frame_idx_global": 0,
            "drill_x": 2.0, "drill_y": 3.01, "drill_z": 4.0,
            "volume_x": 2.0, "volume_y": 3.0, "volume_z": 4.0,
            "volume_qx": 0.0, "volume_qy": 0.0,
            "volume_qz": 0.0, "volume_qw": 1.0,
            "burr_mm": 6.0,
        }]
        original = copy.deepcopy(frames)
        edt = EdtFrameBuild(frames, [], {
            "schema": "synthetic_edt", "ready_for_visibility_enrichment": True,
        })
        record = SimpleNamespace(
            anatomy_key="fixture", global_run_id="SYNTHETIC:CASE",
            dataset_id="SYNTHETIC", run_id="CASE", edt_profile="fixture_grid",
        )
        sources = SimpleNamespace(edt_dir=Path("synthetic_not_opened"))
        # Grid value encodes x,y,z so rotation/index swaps change distances.
        z, y, x = np.indices((10, 10, 10))
        grid = (100 * z + 10 * y + x).astype(np.float32)
        prefix = "evidence_system.frame_pipeline.tip_edt."
        with (
            patch(prefix + "load_anatomy_profile", return_value=profile),
            patch(prefix + "read_edt_file", return_value=(grid, (10, 10, 10))),
            patch(prefix + "_git_commit", return_value="synthetic"),
        ):
            result = enrich_tip_edt(edt, record, sources, project_root=Path("."))
        self.assertEqual(frames, original)
        return result

    def test_calibrated_orientation_translation_scale_index_and_burr_semantics(self) -> None:
        result = self.build(synthetic_profile())
        row = result.frames[0]
        np.testing.assert_allclose(
            [row[f"p_local_sim_{axis}"] for axis in "xyz"], [0.2, 0, 0], atol=1e-13
        )
        self.assertEqual([row[f"idx_FacialNerve_{a}"] for a in "xyz"], [7, 5, 5])
        self.assertAlmostEqual(row["dist_FacialNerve_mm"], 557 * 0.2 - 3)
        self.assertEqual(row["volume_qw"], 1.0)  # Original evidence remains untouched.
        self.assertEqual(result.manifest["phase_signal_geometry"]["schema"], SYNTHETIC_SCHEMA)

    def test_legacy_uses_recorded_quaternion_without_opt_in(self) -> None:
        profile = synthetic_profile()
        del profile["phase_signal_geometry"]
        result = self.build(profile)
        row = result.frames[0]
        np.testing.assert_allclose(
            [row[f"p_local_sim_{axis}"] for axis in "xyz"], [0, 0.2, 0], atol=1e-13
        )
        self.assertEqual([row[f"idx_FacialNerve_{a}"] for a in "xyz"], [5, 3, 5])
        self.assertAlmostEqual(row["dist_FacialNerve_mm"], 535 * 0.2 - 3)
        self.assertIsNone(result.manifest["phase_signal_geometry"])


if __name__ == "__main__":
    unittest.main()
