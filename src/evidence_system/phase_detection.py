"""Offline signal-phase adapter for the frozen E71 MS-TCN++ checkpoint."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .hdf5_clock import SOURCE as NATIVE_CLOCK_SOURCE
from .phase_features import FEATURES, SOURCE_COLUMNS, extract_features, transform_features

PHASES = ["early_phase", "antrum", "incus"]
CANONICAL = {
    "predrilling": "pre_drilling", "early_phase": "early_surface",
    "antrum": "antrum", "incus": "incus",
}


@dataclass
class PhaseBuild:
    frames: list[dict]
    phase_intervals: list[dict]
    manifest: dict


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load_bundle(bundle_dir):
    bundle = Path(bundle_dir).expanduser().resolve()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("schema") != "e71_signal_phase_bundle_v1"
            or manifest.get("phases") != PHASES or manifest.get("features") != FEATURES
            or manifest.get("input_width") != 96 or manifest.get("alpha") != 1.0):
        raise ValueError("Unsupported signal-phase bundle contract")
    for name in ("model.pt", "neural_preprocessing.json"):
        if sha256(bundle / name) != manifest["sha256"].get(name):
            raise ValueError(f"Phase bundle checksum mismatch: {name}")
    preprocessing = json.loads((bundle / "neural_preprocessing.json").read_text(encoding="utf-8"))
    transform_features(np.zeros((1, 48)), preprocessing)
    return bundle, manifest, preprocessing


def assign_intervals(frames, record, times, probabilities, first_removal_s, duration):
    """Hold predictions forward; backfill the first <1s gap after the removal gate."""
    definitions = []
    if first_removal_s is None or first_removal_s > 0:
        definitions.append((0.0, "predrilling", "first_positive_removal_rule"))
    for index, (t, p) in enumerate(zip(times, probabilities, strict=True)):
        label = PHASES[int(np.argmax(p))]
        start = float(first_removal_s) if index == 0 else float(t)
        if not definitions or definitions[-1][1] != label:
            definitions.append((start, label, "model_phase_prediction"))
    intervals = []
    for index, (start, label, phase_source) in enumerate(definitions):
        intervals.append({
            "phase_id": f"{record.global_run_id}:phase:predicted:{index + 1:04d}",
            "phase_order": index + 1, "phase": CANONICAL[label],
            "model_phase": label, "phase_source": phase_source,
            "annotation_start_s": start,
            "annotation_end_s": definitions[index + 1][0] if index + 1 < len(definitions) else float(duration),
            "observed_start_s": None, "observed_end_s": None, "observed_frame_count": 0,
            "boundary_source": phase_source, "phase_annotation_id": None,
        })
    starts = [definition[0] for definition in definitions]
    output = []
    for source in frames:
        row = dict(source)
        t = float(row["t_video_s"])
        if first_removal_s is None or t < first_removal_s:
            label, confidence, endpoint = "predrilling", None, None
            phase_source = "first_positive_removal_rule"
        else:
            index = max(0, min(len(times) - 1, int(np.searchsorted(times, t, side="right")) - 1))
            label = PHASES[int(np.argmax(probabilities[index]))]
            confidence = float(np.max(probabilities[index]))
            endpoint = float(times[index])
            phase_source = "model_phase_prediction"
        row.update(
            phase=CANONICAL[label], model_phase=label, phase_confidence=confidence,
            phase_source=phase_source, phase_prediction_endpoint_s=endpoint,
            phase_annotation_id=None,
        )
        interval = intervals[max(0, int(np.searchsorted(starts, t, side="right")) - 1)]
        row["phase_id"] = interval["phase_id"]
        if interval["observed_start_s"] is None:
            interval["observed_start_s"] = t
        interval["observed_end_s"] = t
        interval["observed_frame_count"] += 1
        output.append(row)
    return output, intervals


def infer_phases(
    frames, record, *, bundle_dir, output_dir,
    geometry_profile=None, clock_manifest=None,
):
    bundle, bundle_manifest, preprocessing = load_bundle(bundle_dir)
    geometry = geometry_profile or {}
    if (geometry.get("schema") not in (
            "r4_signal_geometry_v1", "r4_anatomy_model_geometry_v1", "synthetic_signal_geometry_v1")
            or geometry.get("global_run_id") != record.global_run_id
            or "contract_validation" not in geometry):
        raise ValueError("Automatic phases require validated, run-bound signal geometry")
    if not frames:
        raise ValueError("Cannot predict phases for an empty recording")
    clock = clock_manifest or {}
    if not clock:
        raise ValueError("Automatic phases require an explicit frame/video clock manifest")
    if (clock.get("configured_global_run_id") != record.global_run_id
            or clock.get("frame_count") != len(frames)):
        raise ValueError("Phase clock is not bound to this recording and frame inventory")
    native_clock = clock.get("source") == NATIVE_CLOCK_SOURCE
    if native_clock and (
        clock.get("native_correspondence_verified") is not True
        or clock.get("training_reference_clock") is not False
        or not clock.get("source_inventory_sha256")
        or not clock.get("generated_manifest_sha256")
    ):
        raise ValueError("Generated HDF5 phase clock requires verified source and frame correspondence")
    if (geometry.get("schema") in ("r4_signal_geometry_v1", "r4_anatomy_model_geometry_v1")
            and clock.get("source") not in ("explicit_frame_alignment", NATIVE_CLOCK_SOURCE)):
        raise ValueError(
            "The R4 signal model requires explicit alignment or a verified generated HDF5 clock; "
            "unbound native HDF5 elapsed time is not the training clock"
        )
    duration = float(clock.get("duration_s") or max(float(r["t_video_s"]) for r in frames))
    if not np.isfinite(duration) or duration < max(float(r["t_video_s"]) for r in frames):
        raise ValueError("Phase duration must cover every aligned source frame")
    endpoints = np.arange(1.0, np.floor(duration) + 1.0)
    raw, receipts = extract_features(frames, endpoints)
    counts = np.asarray([row["frame_removed_voxels"] for row in frames], dtype=float)
    if not np.isfinite(counts).all() or np.any(counts < 0):
        raise ValueError("First-removal gate requires observed nonnegative removal counts")
    removed = np.flatnonzero(counts > 0)
    first_removal = float(frames[int(removed[0])]["t_video_s"]) if len(removed) else None
    active = endpoints >= first_removal if first_removal is not None else np.zeros(len(endpoints), bool)
    if first_removal is not None and not active.any():
        raise ValueError("Recording has removal but no complete 1 Hz model endpoint after onset")
    if active.any() and not any(
        np.isfinite(np.asarray([r[f"dist_{s}_mm"] for r in frames], float)).any()
        for s in ("EAC", "FacialNerve", "BonyLabyrinth", "SigSinus", "Incus", "TMJ")
    ):
        raise ValueError("All tip anatomy distances are unavailable; check calibrated geometry")
    x = transform_features(raw[active], preprocessing)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    np.save(output / "model_input.npy", x, allow_pickle=False)
    with (output / "features.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["time_s", "model_active", *FEATURES])
        for t, enabled, values in zip(endpoints, active, raw, strict=True):
            writer.writerow([t, bool(enabled), *[v if np.isfinite(v) else "" for v in values]])
    write_json(output / "feature_receipts.json", receipts)
    runtime = None
    probabilities = np.empty((0, 3), dtype=np.float32)
    if len(x):
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[1])
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, environment.get("PYTHONPATH")]))
        process = subprocess.run([
            sys.executable, "-m", "evidence_system.phase_worker",
            "--bundle", str(bundle), "--input", str(output / "model_input.npy"),
            "--output", str(output / "probabilities.npy"),
        ], env=environment, capture_output=True, text=True, check=False)
        (output / "inference.log").write_text(process.stdout + process.stderr, encoding="utf-8")
        if process.returncode:
            raise RuntimeError("Signal phase inference failed; inspect phase output inference.log")
        probabilities = np.load(output / "probabilities.npy", allow_pickle=False)
        if (probabilities.shape != (len(x), 3) or not np.isfinite(probabilities).all()
                or np.any(probabilities < 0) or not np.allclose(probabilities.sum(1), 1, atol=1e-5)):
            raise ValueError("Invalid model probabilities")
        runtime = json.loads((output / "probabilities.json").read_text(encoding="utf-8"))
    predicted_frames, intervals = assign_intervals(
        frames, record, endpoints[active], probabilities, first_removal, duration,
    )
    with (output / "predictions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["time_s", "model_phase", *[f"probability_{p}" for p in PHASES]])
        for t, p in zip(endpoints[active], probabilities, strict=True):
            writer.writerow([t, PHASES[int(np.argmax(p))], *p.tolist()])
    manifest = {
        "schema": "offline_signal_phase_prediction_v1", "global_run_id": record.global_run_id,
        "source_kind": "model_phase_prediction", "model_id": bundle_manifest["model_id"],
        "bundle_manifest_sha256": sha256(bundle / "manifest.json"),
        "checkpoint_sha256": bundle_manifest["sha256"]["model.pt"],
        "preprocessing_sha256": bundle_manifest["sha256"]["neural_preprocessing.json"],
        "first_removal_s": first_removal, "first_removal_frame": int(removed[0]) if len(removed) else None,
        "duration_s": duration, "clock": clock, "geometry": geometry,
        "native_generated_clock": native_clock,
        "clock_generalization_limit": (
            "Native elapsed time differs from the historical training reference clock; "
            "performance on this clock has not been established" if native_clock else None
        ),
        "recording_geometry_verified": geometry.get("recording_geometry_verified"),
        "source_columns": SOURCE_COLUMNS, "feature_count": 48, "features": FEATURES,
        "raw_feature_shape": list(raw.shape), "model_input_shape": [1, len(x), 96],
        "output_shape": list(probabilities.shape), "runtime": runtime,
        "canonical_label_mapping": CANONICAL, "offline": True,
        "frame_mapping": "Hold previous endpoint; backfill first prediction to first positive removal",
        "interval_semantics": "Contiguous segments; half-open boundaries, final endpoint included",
        "decoding": "Raw final-stage argmax; no smoothing or enforced phase order",
        "trained_modules": [], "frozen_modules": ["MS-TCN++", "training preprocessing"],
        "reference_labels_used": False, "feedback_used_as_input": False, "video_used_as_input": False,
        "language_model_tokens": 0, "user_review_performed": False, "clinician_review_performed": False,
    }
    write_json(output / "phase_intervals.json", intervals)
    write_json(output / "manifest.json", manifest)
    return PhaseBuild(predicted_frames, intervals, manifest)
