"""Export existing five-second and additional ten-second reference distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.motion_comparison import summarize_expert_reference


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--five-second-reference", type=Path, required=True)
    parser.add_argument("--ten-second-reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError("Use a new output filename; saved distributions are preserved")
    summaries = {}
    cohorts = []
    for seconds, path in ((5, args.five_second_reference), (10, args.ten_second_reference)):
        raw = path.read_bytes()
        reference = json.loads(raw)
        if (
            reference["protocol"]["window_s"] != seconds
            or reference["protocol"]["stride_s"] != seconds
        ):
            raise ValueError("Expected matched nonoverlapping five- and ten-second grids")
        cohorts.append(
            {
                recording["global_run_id"]: {
                    "participant_id": recording["participant_id"],
                    "source_artifacts": recording["provenance"]["source_artifacts"],
                    "stroke_lengths": recording.get("stroke_lengths"),
                }
                for recording in reference["recordings"]
            }
        )
        summaries[f"{seconds}s"] = {
            "reference_file": path.name,
            "reference_file_sha256": hashlib.sha256(raw).hexdigest(),
            **summarize_expert_reference(reference),
        }
    if cohorts[0] != cohorts[1]:
        raise ValueError("Reference cohorts, raw sources or complete stroke measurements differ")
    result = {
        "schema": "motion_window_duration_comparison_v1",
        "status": "executed_offline_descriptive_aggregation",
        "same_recordings_sources_and_complete_strokes": True,
        "deployment_changed": False,
        "references": summaries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "window_counts": {
                    key: summary["window_count"] for key, summary in summaries.items()
                },
            }
        )
    )


if __name__ == "__main__":
    main()
