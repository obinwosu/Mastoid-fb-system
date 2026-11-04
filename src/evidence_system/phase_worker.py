"""CPU-only, label-free inference using the pipeline's Python environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from .phase_network import SETTINGS, MSTCNPP


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    checkpoint = torch.load(args.bundle / "model.pt", map_location="cpu", weights_only=True)
    if (checkpoint["Architecture"] != "MSTCNPP" or checkpoint["InputWidth"] != 96
            or list(checkpoint["Phases"]) != ["early_phase", "antrum", "incus"]):
        raise ValueError("Checkpoint does not match the E71 three-class contract")
    if any(checkpoint["Settings"].get(key) != value for key, value in SETTINGS.items()):
        raise ValueError("Checkpoint architecture settings differ from the inference network")
    if checkpoint["Selection"].get("Alpha") != 1.0:
        raise ValueError("This adapter requires the selected raw-probability checkpoint")
    model = MSTCNPP().cpu()
    model.load_state_dict(checkpoint["StateDict"], strict=True)
    model.eval()
    x = np.load(args.input, allow_pickle=False)
    if x.ndim != 2 or x.shape[1] != 96 or not len(x) or not np.isfinite(x).all():
        raise ValueError("Inference input must be a finite nonempty T x 96 array")
    with torch.inference_mode():
        logits = model(torch.from_numpy(x.astype(np.float32))[None])
        probabilities = logits[-1, 0].softmax(-1).numpy()
    np.save(args.output, probabilities, allow_pickle=False)
    args.output.with_suffix(".json").write_text(json.dumps({
        "device": "cpu", "torch_version": torch.__version__,
        "input_shape": [1, len(x), 96], "stage_logits_shape": list(logits.shape),
        "output_shape": list(probabilities.shape), "trained": False,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
