"""Package an existing E71 checkpoint and exact scaler; never train or download."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.phase_detection import PHASES, load_bundle, sha256, write_json  # noqa: E402
from evidence_system.phase_features import FEATURES  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--preprocessing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.checkpoint, args.output / "model.pt")
    shutil.copy2(args.preprocessing, args.output / "neural_preprocessing.json")
    write_json(args.output / "manifest.json", {
        "schema": "e71_signal_phase_bundle_v1", "model_id": args.model_id,
        "architecture": "MSTCNPP", "phases": PHASES, "features": FEATURES,
        "input_width": 96, "sample_hz": 1, "alpha": 1.0,
        "sha256": {name: sha256(args.output / name) for name in ("model.pt", "neural_preprocessing.json")},
        "removal_gate": "First source frame_removed_voxels > 0; latch active permanently",
        "removal_unit": "Source metadata reported mm3, not independently recalibrated physical volume",
        "anatomy_unit": "R4 burr-surface clearance mm; removed-region minimum EDT mm, not deepest point",
        "features_contract": "E68 base33 + E69 removed-region15; trailing (t-width,t], prefix <=t",
        "preprocessing": "log1p removal3; frozen median imputation and z score; append48 missing flags",
        "offline": True, "context_radius_samples": 5053, "inference_only": True,
    })
    load_bundle(args.output)
    print(json.dumps({"bundle": str(args.output.resolve()), "model_id": args.model_id}))


if __name__ == "__main__":
    main()
