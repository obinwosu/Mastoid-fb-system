"""Versioned E71 signal features; no labels, video appearance or motion inputs."""

from __future__ import annotations

import numpy as np

STRUCTURES = ("EAC", "FacialNerve", "BonyLabyrinth", "SigSinus", "Incus", "TMJ")
REMOVED_STRUCTURES = ("EAC", "FacialNerve", "BonyLabyrinth", "SigSinus", "TMJ")
REMOVAL = ["source_removed_cumulative", "source_removed_sum_5s", "source_removed_sum_30s"]
FEATURES = (
    REMOVAL
    + [
        f"clearance_{s}_{stat}"
        for s in STRUCTURES
        for stat in ("current", "median_5s", "min_5s", "median_30s", "min_30s")
    ]
    + [
        f"removed_clearance_{s}_{stat}"
        for s in REMOVED_STRUCTURES
        for stat in ("cumulative_min", "min_5s", "min_30s")
    ]
)
SOURCE_COLUMNS = [
    "frame_removed_voxels",
    "frame_removed_mm3",
    "frame_removed_mm3_cum",
    *[f"dist_{s}_mm" for s in STRUCTURES],
    *[f"voxel_min_{s}_mm" for s in REMOVED_STRUCTURES],
]


def extract_features(frames, endpoints):
    """Match frozen E68/E69 definitions, including empty windows and stale rows."""
    if not frames:
        raise ValueError("Signal phase detection requires frame rows")
    for key in ["t_video_s", *SOURCE_COLUMNS]:
        if any(key not in row for row in frames):
            raise ValueError(f"Missing phase source column: {key}")
    t = np.asarray([row["t_video_s"] for row in frames], dtype=float)
    q = np.asarray(endpoints, dtype=float)
    if not np.isfinite(t).all() or np.any(np.diff(t) < 0):
        raise ValueError("Phase source clock must be finite and ordered")
    data = {key: np.asarray([r[key] for r in frames], dtype=float) for key in SOURCE_COLUMNS}
    output = np.full((len(q), len(FEATURES)), np.nan)
    receipts = []
    for n, end in enumerate(q):
        hi = int(np.searchsorted(t, end, side="right"))
        fresh = hi > 0 and end - t[hi - 1] <= 1.0 + 1e-9
        lows = {w: int(np.searchsorted(t, end - w, side="right")) for w in (5, 30)}
        if fresh:
            output[n, 0] = data["frame_removed_mm3_cum"][hi - 1]
        for j, w in enumerate((5, 30), 1):
            a = data["frame_removed_mm3"][lows[w] : hi]
            if np.isfinite(a).any():
                output[n, j] = np.nansum(a)
        for j, s in enumerate(STRUCTURES):
            a = data[f"dist_{s}_mm"]
            start = 3 + 5 * j
            if fresh:
                output[n, start] = a[hi - 1]
            for offset, w in ((1, 5), (3, 30)):
                window = a[lows[w] : hi]
                window = window[np.isfinite(window)]
                if len(window):
                    output[n, start + offset : start + offset + 2] = [
                        np.median(window),
                        np.min(window),
                    ]
        for j, s in enumerate(REMOVED_STRUCTURES):
            a = data[f"voxel_min_{s}_mm"]
            for offset, start in enumerate((0, lows[5], lows[30])):
                window = a[start:hi]
                window = window[np.isfinite(window)]
                if len(window):
                    output[n, 33 + 3 * j + offset] = np.min(window)
        receipts.append(
            {
                "time_s": float(end),
                "source_end_row_exclusive": hi,
                "source_start_row_5s": lows[5],
                "source_start_row_30s": lows[30],
                "latest_source_video_s": float(t[hi - 1]) if hi else None,
                "current_fresh": bool(fresh),
            }
        )
    output[~np.isfinite(output)] = np.nan
    return output, receipts


def transform_features(raw, preprocessing):
    if preprocessing["Columns"] != FEATURES or preprocessing["Log1pColumns"] != REMOVAL:
        raise ValueError("Bundle feature ordering does not match E71 signal contract")
    statistics = [
        np.asarray(preprocessing[key], dtype=float) for key in ("Medians", "Means", "Std")
    ]
    if any(a.shape != (48,) or not np.isfinite(a).all() for a in statistics):
        raise ValueError("Invalid phase preprocessing statistics")
    medians, means, std = statistics
    if np.any(std <= 0):
        raise ValueError("Phase preprocessing standard deviations must be positive")
    logged = np.asarray(raw, dtype=float).copy()
    if np.any(logged[:, :3] < 0):
        raise ValueError("Source removal amounts must be nonnegative")
    logged[:, :3] = np.log1p(logged[:, :3])
    missing = ~np.isfinite(logged)
    filled = np.where(missing, medians, logged)
    return np.column_stack(((filled - means) / std, missing)).astype(np.float32)
