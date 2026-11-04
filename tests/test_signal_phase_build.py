"""Signal-phase orchestration contracts on synthetic data; no model training."""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from evidence_system.cli import build_parser  # noqa: E402
from evidence_system.coaching_plan import build_session_coaching_plan  # noqa: E402
from evidence_system.frame_pipeline.core_frames import (  # noqa: E402
    REMOVED_VOXEL_FIELDS,
    extract_core_frames,
    map_native_event_times,
    resolve_frame_clock,
    write_core_frame_build,
)
from evidence_system.pipeline import build_evidence  # noqa: E402
from evidence_system.registry import get_run, load_registry  # noqa: E402
from evidence_system.source_audit import build_source_audit  # noqa: E402
from evidence_system.source_config import (  # noqa: E402
    SourceRoots,
    load_source_roots,
    resolve_run_sources,
)
from evidence_system.web import FeedbackService  # noqa: E402
from scripts.create_demo_data import create_demo_data  # noqa: E402

DEPENDENCIES = all(
    importlib.util.find_spec(name) is not None for name in ("h5py", "numpy", "scipy", "cv2")
)


def write_clock(path, count, *, extra_phase="not_an_input", times=None):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["frame_idx_global", "video_time_s", "world_frame_index", "phase"]
        )
        writer.writeheader()
        for index in range(count):
            writer.writerow(
                {
                    "frame_idx_global": index,
                    "video_time_s": times[index] if times is not None else index * 0.2,
                    "world_frame_index": 2 * index,
                    "phase": extra_phase,
                }
            )


class SignalConfigurationTests(unittest.TestCase):
    def test_git_is_optional_for_build_provenance(self):
        from evidence_system.frame_pipeline.core_frames import _git_commit

        with patch(
            "evidence_system.frame_pipeline.core_frames.subprocess.run",
            side_effect=FileNotFoundError("git is not installed"),
        ):
            self.assertEqual(_git_commit(ROOT), "unknown")

    def test_bundle_config_ignores_legacy_interpreter_and_cli_uses_current_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = folder / "source.json"
            config.write_text(
                json.dumps(
                    {
                        "data_root": "raw",
                        "edt_root": "edt",
                        "phase_model_bundle": "bundle",
                        "phase_python": "runtime/python",
                        "frame_alignment_file": "clock.csv",
                        "phase_duration_s": 12.5,
                    }
                )
            )
            roots = load_source_roots(config, project_root=folder)
            self.assertIsNone(roots.phase_annotation_file)
            self.assertEqual(roots.phase_model_bundle, folder / "bundle")
            self.assertEqual(roots.phase_duration_s, 12.5)
            self.assertFalse(hasattr(roots, "phase_python"))
            args = build_parser().parse_args(
                [
                    "build",
                    "--run-id",
                    "DEMO_CASE_01",
                    "--phase-model-bundle",
                    "bundle",
                    "--frame-alignment-file",
                    "clock.csv",
                    "--phase-duration-s",
                    "12.5",
                ]
            )
            self.assertEqual(args.phase_model_bundle, Path("bundle"))
            self.assertEqual(args.phase_duration_s, 12.5)
            self.assertFalse(hasattr(args, "phase_python"))

    def test_manual_config_still_requires_an_annotation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = folder / "source.json"
            config.write_text(json.dumps({"data_root": "raw", "edt_root": "edt"}))
            with self.assertRaisesRegex(ValueError, "phase_annotation_file"):
                load_source_roots(config, project_root=folder)
            roots = load_source_roots(
                config, project_root=folder, phase_model_bundle=folder / "bundle"
            )
            self.assertIsNone(roots.phase_annotation_file)


class ClockAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.frames = [
            {"t_abs": 100.0 + i * 0.1, "source_hdf5": "synthetic.hdf5", "source_frame_idx": i}
            for i in range(4)
        ]

    def test_clock_columns_only_and_explicit_duration_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "clock.csv"
            write_clock(path, 4, extra_phase="a_reference_label_that_must_not_enter_inputs")
            clock, manifest = resolve_frame_clock(
                self.frames, path, global_run_id="DEMO:CASE", duration_s=0.9
            )
            self.assertEqual(
                [row["video_time_s"] for row in clock], [0.0, 0.2, 0.4, 0.6000000000000001]
            )
            self.assertTrue(all(set(row) == {"video_time_s", "world_frame_index"} for row in clock))
            self.assertFalse(manifest["phase_columns_consumed"])
            self.assertEqual(manifest["configured_global_run_id"], "DEMO:CASE")
            self.assertEqual(manifest["duration_s"], 0.9)
            self.assertEqual(manifest["duration_source"], "explicit_recording_duration")
            self.assertEqual(len(manifest["sha256"]), 64)
            write_clock(path, 4, extra_phase="different_ignored_reference")
            changed, changed_manifest = resolve_frame_clock(self.frames, path)
            self.assertEqual(changed, clock)
            self.assertNotEqual(changed_manifest["sha256"], manifest["sha256"])

    def test_count_order_nonfinite_and_short_duration_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "clock.csv"
            write_clock(path, 3)
            with self.assertRaisesRegex(ValueError, "count"):
                resolve_frame_clock(self.frames, path)
            write_clock(path, 4, times=[0.0, 0.2, 0.2, 0.6])
            with self.assertRaisesRegex(ValueError, "strictly increase"):
                resolve_frame_clock(self.frames, path)
            write_clock(path, 4, times=[0.0, 0.2, float("nan"), 0.6])
            with self.assertRaisesRegex(ValueError, "finite"):
                resolve_frame_clock(self.frames, path)
            write_clock(path, 4)
            with self.assertRaisesRegex(ValueError, "duration"):
                resolve_frame_clock(self.frames, path, duration_s=0.5)
            text = path.read_text().replace("1,0.2,2,", "2,0.2,2,")
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, "indices"):
                resolve_frame_clock(self.frames, path)

    @unittest.skipUnless(DEPENDENCIES, "requires numeric pipeline dependencies")
    def test_event_interpolation_and_endpoint_extrapolation_keep_native_join(self):
        import numpy as np

        mapped = map_native_event_times(
            [9.0, 10.0, 10.5, 12.0, 13.0], [10.0, 11.0, 12.0], [2.0, 4.0, 7.0]
        )
        np.testing.assert_allclose(mapped, [0.0, 2.0, 3.0, 7.0, 10.0])


@unittest.skipUnless(DEPENDENCIES, "requires numeric pipeline dependencies")
class SignalBuildTests(unittest.TestCase):
    def fixture(self, folder):
        fixture = create_demo_data(folder / "demo")
        record = get_run("DEMO_CASE_01", load_registry(ROOT / "config/examples/runs.example.csv"))
        bundle = folder / "bundle"
        bundle.mkdir()
        for name in ("model.pt", "neural_preprocessing.json", "manifest.json"):
            (bundle / name).write_text("synthetic test stub; no model weights")
        alignment = folder / "clock.csv"
        write_clock(alignment, 24)
        roots = SourceRoots(
            Path(fixture["output_root"]) / "raw",
            Path(fixture["output_root"]) / "edt",
            phase_model_bundle=bundle,
            frame_alignment_file=alignment,
            phase_duration_s=5.0,
        )
        sources = resolve_run_sources(record, roots)
        profiles = json.loads((ROOT / "config/examples/anatomy_profiles.example.json").read_text())
        profile = profiles["profiles"][record.anatomy_key]
        profile["phase_signal_geometry"] = {
            "schema": "synthetic_signal_geometry_v1",
            "global_run_id": record.global_run_id,
            "edt_profile": record.edt_profile,
            "actual_specimen": "synthetic_demo",
            "calibration_recording_id": "synthetic_demo",
            "rotation_source": "synthetic fixture identity",
            "volume_quaternion_xyzw": [0.0, 0.0, 0.0, 1.0],
            "recorder_scale_m_per_simulator_unit": profile["meters_per_simulator_unit"],
            "edt_distance_scale_mm": profile["edt_distance_scale_mm"],
            "volume_dimensions_sim": profile["volume_dimensions_sim"],
            "geometry_quality": "synthetic",
        }
        profile_path = folder / "profiles.json"
        profile_path.write_text(json.dumps(profiles))
        return fixture, record, sources, profile_path

    def test_core_defers_phases_and_keeps_raw_time_without_reading_annotation(self):
        import numpy as np

        with tempfile.TemporaryDirectory() as temporary:
            fixture, record, sources, _ = self.fixture(Path(temporary))
            Path(fixture["phase_annotation_file"]).unlink()
            with patch(
                "evidence_system.frame_pipeline.core_frames.inspect_phase_annotation",
                side_effect=AssertionError("manual input read"),
            ):
                core = extract_core_frames(record, sources, project_root=ROOT)
            self.assertEqual(core.phase_intervals, [])
            self.assertTrue(
                all(
                    row["phase"] is None and row["phase_annotation_id"] is None
                    for row in core.frames
                )
            )
            np.testing.assert_allclose([row["t_abs"] for row in core.frames], np.arange(24) * 0.1)
            np.testing.assert_allclose(
                [row["t_video_s"] for row in core.frames], np.arange(24) * 0.2
            )
            self.assertEqual(core.manifest["clock_alignment"]["duration_s"], 5.0)
            for voxel in core.removed_voxels:
                self.assertAlmostEqual(voxel["t_video_s"], voxel["t_abs"] * 2)
            with patch(
                "evidence_system.source_audit.inspect_phase_annotation",
                side_effect=AssertionError("manual input read"),
            ):
                audit = build_source_audit(record, sources, hash_hdf5=False)
            self.assertTrue(audit["ready_for_frame_extraction"])
            self.assertEqual(audit["phase_annotation"]["status"], "pending_inference")
            self.assertNotIn("phase_annotation_unique_and_valid", audit["checks"])

    def test_end_to_end_dispatch_provenance_and_shared_video_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fixture, record, sources, profiles = self.fixture(folder)
            Path(fixture["phase_annotation_file"]).unlink()
            calls = []

            def fake_infer(frames, actual_record, **kwargs):
                self.assertEqual(actual_record, record)
                self.assertIsNone(frames[0]["phase"])
                self.assertIn("dist_FacialNerve_mm", frames[0])
                self.assertIn("voxel_min_EAC_mm", frames[0])
                self.assertEqual(
                    kwargs["geometry_profile"]["schema"], "synthetic_signal_geometry_v1"
                )
                self.assertEqual(kwargs["clock_manifest"]["duration_s"], 5.0)
                calls.append(kwargs)
                output = [dict(row) for row in frames]
                intervals = []
                segments = [
                    (0, 2, "predrilling", "pre_drilling"),
                    (2, 8, "early_phase", "early_surface"),
                    (8, 14, "antrum", "antrum"),
                    (14, 18, "incus", "incus"),
                    (18, 21, "antrum", "antrum"),
                    (21, 24, "incus", "incus"),
                ]
                for number, (first, stop, model_phase, canonical) in enumerate(segments):
                    phase_id = f"{record.global_run_id}:model_phase:{number:06d}"
                    interval = {
                        "phase_id": phase_id,
                        "phase_order": number + 1,
                        "phase": canonical,
                        "model_phase": model_phase,
                        "annotation_start_s": output[first]["t_video_s"],
                        "annotation_end_s": output[stop]["t_video_s"]
                        if stop < len(output)
                        else 5.0,
                        "observed_start_s": output[first]["t_video_s"],
                        "observed_end_s": output[stop - 1]["t_video_s"],
                        "observed_frame_count": stop - first,
                        "boundary_source": "model_phase_prediction",
                        "phase_source": "model_phase_prediction",
                        "phase_annotation_id": None,
                    }
                    intervals.append(interval)
                    for row in output[first:stop]:
                        row.update(
                            phase=canonical,
                            model_phase=model_phase,
                            phase_id=phase_id,
                            phase_source="model_phase_prediction",
                            phase_annotation_id=None,
                        )
                manifest = {
                    "source_kind": "model_phase_prediction",
                    "model_id": "synthetic_test_stub",
                    "first_removal_s": output[2]["t_video_s"],
                    "clock_alignment": kwargs["clock_manifest"],
                }
                kwargs["output_dir"].mkdir(parents=True)
                (kwargs["output_dir"] / "manifest.json").write_text(json.dumps(manifest))
                return types.SimpleNamespace(
                    frames=output, phase_intervals=intervals, manifest=manifest
                )

            with (
                patch("evidence_system.phase_detection.infer_phases", side_effect=fake_infer),
                patch.dict(os.environ, {"MASTOID_ANATOMY_PROFILES": str(profiles)}),
                patch(
                    "evidence_system.pipeline.build_review_video",
                    return_value={"video_file": "synthetic.webm"},
                ) as video,
            ):
                summary = build_evidence(
                    record=record,
                    sources=sources,
                    result_dir=folder / "out",
                    project_root=ROOT,
                    build_video=True,
                )
            self.assertEqual(len(calls), 1)
            self.assertEqual(summary["phase_source_kind"], "model_phase_prediction")
            self.assertEqual(summary["store_integrity"], "ok")
            self.assertEqual(summary["under_ledge_detection"]["status"], "completed")
            ray_dir = folder / "out/stages/08_under_ledge_ray"
            ray_manifest = json.loads((ray_dir / "ray_v3_summary.json").read_text())
            self.assertEqual(ray_manifest["run_id"], record.run_id)
            self.assertEqual(ray_manifest["clock_alignment"]["duration_s"], 5.0)
            self.assertEqual(ray_manifest["phase_source_kind"], "model_phase_prediction")
            self.assertEqual(len(ray_manifest["input_artifacts"]["frames"]["sha256"]), 64)
            with (ray_dir / "ray_v3_frame_scores.csv").open(newline="") as stream:
                ray_rows = list(csv.DictReader(stream))
            self.assertEqual(len(ray_rows), 24)
            self.assertEqual(
                [float(row["t_video_s"]) for row in ray_rows], [i * 0.2 for i in range(24)]
            )
            self.assertEqual(
                video.call_args.kwargs["frame_times_video"], [index * 0.2 for index in range(24)]
            )
            self.assertEqual(video.call_args.kwargs["clock_manifest"]["duration_s"], 5.0)
            audit = json.loads(
                (folder / "out/stages/00_source_audit/source_manifest.json").read_text()
            )
            self.assertEqual(audit["phase_annotation"]["status"], "completed_model_prediction")
            self.assertFalse(audit["phase_annotation"]["human_annotation_read"])
            with (folder / "out/stages/01_core_frames/frame_observations_core.csv").open(
                newline=""
            ) as stream:
                persisted_core = list(csv.DictReader(stream))
            self.assertEqual(persisted_core[8]["phase"], "antrum")
            self.assertNotIn("dist_FacialNerve_mm", persisted_core[0])
            self.assertEqual(persisted_core[8]["phase_source"], "model_phase_prediction")
            for relative in (
                "02_edt_enrichment/frame_observations_edt.csv",
                "03_tip_edt/frame_observations_tip_edt.csv",
            ):
                with (folder / "out/stages" / relative).open(newline="") as stream:
                    persisted_stage = list(csv.DictReader(stream))
                self.assertEqual(
                    [row["phase_id"] for row in persisted_stage],
                    [row["phase_id"] for row in persisted_core],
                )
            core_manifest = json.loads(
                (folder / "out/stages/01_core_frames/extraction_manifest.json").read_text()
            )
            self.assertEqual(core_manifest["phase_status"], "completed_model_prediction")
            self.assertEqual(core_manifest["first_drilling_s"], 0.4)
            service = FeedbackService(
                mode="expert-study",
                db_path=Path(summary["evidence_database"]),
                run_id=record.run_id,
                video_path=None,
                video_manifest_path=None,
                ray_v3_dir=None,
                cohort_benchmark_path=None,
                study_state_path=folder / "synthetic_study.json",
                available_runs=[],
                display_name="Synthetic test",
                interface_version="test",
            )
            with patch(
                "evidence_system.web.build_session_coaching_plan", wraps=build_session_coaching_plan
            ) as coaching:
                bootstrap = service.bootstrap()
            self.assertTrue(bootstrap["detection_status"]["under_ledge"]["available"])
            self.assertEqual(
                bootstrap["detection_status"]["under_ledge"]["source"], "generated_sibling"
            )
            self.assertTrue(coaching.call_args.args[2]["available"])
            supplied_phases = coaching.call_args.args[1]
            self.assertTrue(all(row["phase_id"] for row in supplied_phases))
            self.assertTrue(
                all(row["phase_source_kind"] == "model_phase_prediction" for row in supplied_phases)
            )
            service.start_study("SYNTHETIC_REVIEWER")
            experience = service.analyze_study("SYNTHETIC_REVIEWER")
            for visible, original in zip(experience["phases"], bootstrap["phases"], strict=False):
                for key in ("phase_id", "phase_source_kind", "phase_source_sha256", "segment_role"):
                    self.assertEqual(visible[key], original[key])
            self.assertEqual(experience["phases"][0]["segment_role"], "pre_drilling_context")
            self.assertEqual(experience["phases"][1]["segment_role"], "predicted_drilling_phase")
            with closing(sqlite3.connect(summary["evidence_database"])) as db:
                kinds = [row[0] for row in db.execute("SELECT source_type FROM source_files")]
                self.assertIn("model_phase_prediction", kinds)
                self.assertNotIn("human_phase_annotation", kinds)
                phases = db.execute("SELECT phase_id FROM frames ORDER BY frame_idx").fetchall()
                self.assertNotEqual(phases[8], phases[18])
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_missing_bundle_asset_fails_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, record, sources, _ = self.fixture(Path(temporary))
            (sources.phase_model_bundle / "model.pt").unlink()
            with self.assertRaises(FileNotFoundError):
                build_source_audit(record, sources, hash_hdf5=False)

    def test_no_removal_writes_header_only_but_empty_frames_remain_invalid(self):
        import h5py
        import numpy as np

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fixture, record, sources, _ = self.fixture(folder)
            with h5py.File(fixture["hdf5_file"], "r+") as handle:
                del handle["voxels_removed/voxel_removed"]
                del handle["voxels_removed/voxel_time_stamp"]
                handle.create_dataset(
                    "voxels_removed/voxel_removed", data=np.empty((0, 4), dtype=np.int32)
                )
                handle.create_dataset(
                    "voxels_removed/voxel_time_stamp", data=np.empty(0, dtype=float)
                )
            core = extract_core_frames(record, sources, project_root=ROOT)
            self.assertEqual(core.removed_voxels, [])
            self.assertIsNone(core.manifest["first_drilling_s"])
            write_core_frame_build(folder / "core", core, {"available": False})
            with (folder / "core/removed_voxel_observations.csv").open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, list(REMOVED_VOXEL_FIELDS))
                self.assertEqual(list(reader), [])
            core.frames = []
            with self.assertRaisesRegex(
                ValueError, "Cannot write empty artifact: frame_observations_core"
            ):
                write_core_frame_build(folder / "invalid", core, {"available": False})


if __name__ == "__main__":
    unittest.main()
