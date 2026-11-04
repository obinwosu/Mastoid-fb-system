"""Synthetic authority fixtures test production rejection without real datasets."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from evidence_system.r4_provenance import (  # noqa: E402
    assert_geometry_sources_unchanged,
    validate_production_geometry,
)
from evidence_system.source_audit import REQUIRED_EDT_STEMS  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        generated_recording_id = "P" + "9" * 3 + "T" + "9" * 3
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        simulator = self.root / "simulator"
        simulator.mkdir()
        self.gui = simulator / "gui.yaml"
        self.adf = simulator / "volume_256_TESTSPECIMEN.yaml"
        self.edt = simulator / "edt_fixture"
        self.edt.mkdir()
        self.gui.write_text(
            "volumes: [v]\nv:\n  adf_path: volume_256_TESTSPECIMEN.yaml\n"
            "  sdf_path: /edt_fixture/\n  unit: 0.064\n  name: Anatomy Z\n"
        )
        self.adf.write_text(
            "volumes: [volume]\nvolume:\n  dimensions: {x: 0.256, y: 0.256, z: 0.256}\n"
            "  location:\n    orientation: {r: 0, p: 0, y: 0}\n"
        )
        header = b"G3\n1 FLOAT\n4 4 4\n" + b"0 " * 15 + b"1\n"
        for stem in REQUIRED_EDT_STEMS:
            (self.edt / (stem + ".edt")).write_bytes(header + b"\0" * (4 * 4 * 4 * 4))
        self.table = self.root / "synthetic_authority.csv"
        row = {
            "RecordingID": generated_recording_id,
            "FolderName": "generated_" + generated_recording_id,
            "ActualSpecimen": "TESTSPECIMEN",
            "ActualAnatomy": "Z",
            "UsedRotation": "[0,0,0,1]",
            "FittedRotation": "[0,0,0,1]",
            "RecordedRotation": "[0,0,0,1]",
            "RecorderScale": "0.049202688",
            "SpacingMm": "1.0",
            "GUIUnit": "0.064",
            "ReviewStatus": "needs_review",
            "RotationSource": "ADF/volume_256_TESTSPECIMEN.yaml:current",
        }
        with self.table.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        self.raw = self.root / row["FolderName"]
        self.raw.mkdir()
        self.record = SimpleNamespace(
            dataset_id="R4",
            run_id=generated_recording_id,
            global_run_id="R4:" + generated_recording_id,
            anatomy_key="fixture",
            edt_profile="edt_fixture",
            raw_run_relpath=self.raw.name,
            anatomy_mapping_status="configured",
            source_status="read_only_fixture_for_authority_tests",
        )
        self.sources = SimpleNamespace(raw_run_dir=self.raw, edt_dir=self.edt)
        contract = {
            "schema": "r4_signal_geometry_v1",
            "global_run_id": self.record.global_run_id,
            "edt_profile": "edt_fixture",
            "actual_specimen": "TESTSPECIMEN",
            "calibration_recording_id": self.record.run_id,
            "rotation_source": row["RotationSource"],
            "volume_quaternion_xyzw": [0, 0, 0, 1],
            "recorder_scale_m_per_simulator_unit": 0.049202688,
            "edt_distance_scale_mm": 1.0,
            "volume_dimensions_sim": [0.256] * 3,
            "geometry_quality": "needs_review",
            "grid_resolution_xyz": [4] * 3,
            "source_correction_table": str(self.table),
            "correction_table_sha256": digest(self.table),
            "source_simulator_root": str(simulator),
            "source_gui_path": str(self.gui),
            "source_gui_sha256": digest(self.gui),
            "source_adf_path": str(self.adf),
            "source_adf_sha256": digest(self.adf),
            "source_edt_sha256": {p.name: digest(p) for p in self.edt.glob("*.edt")},
        }
        self.profile = {
            "edt_profile": "edt_fixture",
            "meters_per_simulator_unit": 0.049202688,
            "edt_distance_scale_mm": 1.0,
            "simulator_unit": 0.064,
            "volume_dimensions_sim": [0.256] * 3,
            "adf_profile": str(self.adf),
            "phase_signal_geometry": contract,
        }
        self.config = self.root / "profiles.json"
        self.catalogue = self.root / "edt_source_catalogue.json"
        self.write_catalogue()

    def write_catalogue(self):
        """Explicitly anchor generated source identities for authority tests."""
        contract = self.profile["phase_signal_geometry"]
        self.catalogue.write_text(
            json.dumps(
                {
                    "schema": "r4_edt_sources_v1",
                    "correction_table_sha256": digest(self.table),
                    "models": {
                        "TESTSPECIMEN": {
                            "edt_profile": self.edt.name,
                            "grid_resolution_xyz": contract["grid_resolution_xyz"],
                            "sha256": contract["source_edt_sha256"],
                        }
                    },
                }
            )
        )

    def validate(self, trust_fixture=True):
        self.config.write_text(json.dumps({"profiles": {"fixture": self.profile}}))
        anchors = {digest(self.table)} if trust_fixture else set()
        with (
            patch.dict(os.environ, {"MASTOID_ANATOMY_PROFILES": str(self.config)}),
            patch("evidence_system.r4_provenance.TRUSTED_R4_CORRECTION_SHA256", anchors),
            patch("evidence_system.r4_provenance.EDT_CATALOG_PATH", self.catalogue),
            patch(
                "evidence_system.r4_provenance.TRUSTED_EDT_CATALOG_SHA256", digest(self.catalogue)
            ),
        ):
            return validate_production_geometry(self.record, self.sources, project_root=self.root)

    def test_verified_sources_keep_review_flags_and_exact_grid(self):
        result = self.validate()
        self.assertEqual(result.manifest["status"], "verified_R4_v3_sources")
        self.assertEqual(result.manifest["geometry_quality"], "needs_review")
        self.assertFalse(result.manifest["scientific_all_recording_geometry"])
        self.assertFalse(result.manifest["clinician_review_performed"])
        self.assertEqual(len(result.manifest["source_files"]), 21)
        assert_geometry_sources_unchanged(result)

    def test_untrusted_table_and_real_manual_legacy_fallback_rejected(self):
        with self.assertRaisesRegex(ValueError, "trusted R4"):
            self.validate(trust_fixture=False)
        del self.profile["phase_signal_geometry"]
        with self.assertRaisesRegex(ValueError, "source-verified R4"):
            self.validate()

    def test_wrong_unit_quaternion_is_rejected_against_authority(self):
        self.profile["phase_signal_geometry"]["volume_quaternion_xyzw"] = [0, 0, 1, 0]
        with self.assertRaisesRegex(ValueError, "UsedRotation"):
            self.validate()

    def test_self_consistent_wrong_spacing_is_rejected(self):
        self.profile["edt_distance_scale_mm"] = 2
        self.profile["phase_signal_geometry"]["edt_distance_scale_mm"] = 2
        with self.assertRaisesRegex(ValueError, "SpacingMm"):
            self.validate()

    def test_wrong_raw_folder_and_edt_directory_are_rejected(self):
        self.sources.raw_run_dir = self.root / "other"
        with self.assertRaisesRegex(ValueError, "source folder"):
            self.validate()
        self.sources.raw_run_dir = self.raw
        self.sources.edt_dir = self.root / "other_edt"
        with self.assertRaisesRegex(ValueError, "EDT directory"):
            self.validate()

    def test_changed_edt_and_adf_bytes_fail_before_analysis(self):
        bone = self.edt / "Bone.edt"
        original = bone.read_bytes()
        bone.write_bytes(original[:-1] + b"1")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.validate()
        bone.write_bytes(original)
        self.adf.write_text(self.adf.read_text() + "# changed\n")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.validate()

    def test_post_validation_source_replacement_fails(self):
        result = self.validate()
        self.gui.write_text(self.gui.read_text() + "# changed\n")
        with self.assertRaisesRegex(ValueError, "changed during"):
            assert_geometry_sources_unchanged(result)

    def test_real_identity_cannot_claim_synthetic_exception(self):
        del self.profile["phase_signal_geometry"]
        self.profile["provenance"] = "deterministically generated synthetic fixture"
        self.record.anatomy_mapping_status = "synthetic"
        self.record.source_status = "generated"
        with self.assertRaisesRegex(ValueError, "cannot use the synthetic exception"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
