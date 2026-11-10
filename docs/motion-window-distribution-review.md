# Additional ten-second motion distributions

Executed 2026-11-09 at the author's request. The application continues to use
`references/r4_motion_windows_v6.json` and five-second nonoverlapping windows.
The ten-second result is an additional offline descriptive aggregation.
No page, active configuration, detector threshold, model or prior reference changed.

Updated after the author's clarification: **retain an entire observed phase when
it is shorter than ten seconds**, using its actual duration. The latest result is
v2; the initial full-window-only v1 remains preserved.

## Saved artifacts and reproduction

- [Latest reference: ten seconds or a whole shorter phase](../references/r4_motion_windows_10s_v2.json):
  complete recording-level windows and original complete-stroke measurements.
  File SHA256: `d5913b47a7bf3e8722cfb42b76d22e3a4379945973245507050abdc93a66e54e`.
- [Latest five/ten-second distributions](../references/r4_motion_distribution_comparison_5s_10s_v2.json):
  per-phase values, cumulative weights, counts and weighted 10th/25th/50th/75th/90th
  percentiles for speed, removal rate and mean complete-stroke length.
- Initial full-window-only [v1 reference](../references/r4_motion_windows_10s_v1.json)
  and [v1 distributions](../references/r4_motion_distribution_comparison_5s_10s_v1.json)
  remain unchanged for historical comparison.
- [Questionnaire and review follow-up](resident-study-changes.md#author-follow-up-and-additional-review-2026-11-09).

Run from the application root with the project environment. Replace
`<R4_CATALOG>` with the configured published catalog location and use new output
filenames when rerunning; existing artifacts are not overwritten.

```text
python run.py build-motion-reference --catalog "<R4_CATALOG>" --output references/r4_motion_windows_10s_v2.json --window-seconds 10 --include-short-phases
python scripts/compare_motion_distributions.py --five-second-reference references/r4_motion_windows_v6.json --ten-second-reference references/r4_motion_windows_10s_v2.json --output references/r4_motion_distribution_comparison_5s_10s_v2.json
```

The first command verifies the frozen R4 manifest and every input CSV checksum.
It does not regenerate upstream CSVs or segment strokes again. The second verifies
both artifacts and exact agreement of recording/participant identities, source
declarations and all complete-stroke measurement rows before exporting distributions.

## Measurement contract

Both references contain the same 39 recordings from five participants with
published TrainingYears >= 11; one other expert recording has incomplete manual
phase boundaries and remains excluded. The same 4,710 source stroke rows are
preserved. Specimens and assistance conditions remain pooled; geometry review
flags and historical source exclusions remain unchanged.

The latest protocol is `motion_window_protocol_v4`: nominal window 10 s, stride
10 s, with `short_phase_policy=whole_observed_phase_if_shorter_than_window`.
Each grid starts at the first observed row of a contiguous phase interval.
Windows never cross a phase boundary. A positive-duration phase shorter than
10 s becomes one sample covering its entire observed span. For example, a 7 s
phase contributes a 7 s sample. For phases at least 10 s long, the existing full
10 s grid remains: a 23 s phase still contributes two 10 s windows, with its 3 s
tail omitted. Zero-duration/single-frame phases have no measurable rate window.
There is no padding or fabricated observation. Every v2 window records its kind,
actual duration and observed phase bounds. The observed phase begins after the
existing first-removal gate; these durations are not independent clinical onset
measurements. The historical `motion_window_protocol_v3` remains full-window-only.
At least 80% valid pose coverage is required, and gaps longer than 0.25 s are
not bridged.

- Speed: valid clipped adjacent path in mm divided by valid observed seconds.
- Removal rate: eligible frame-end removal volume in mm3 divided by the actual
  window duration, with separate coverage eligibility. Coverage, control fraction
  and endpoint frequency also use that actual duration, not a nominal 10 s for
  short phases.
- Stroke length: mean physical length of complete eligible recorded strokes whose
  endpoints lie in (start, end]. Whole paths are retained even when they begin
  before the window. No eligible endpoints means missing, not zero.
- Recorded endpoint frequency and legacy drilling-time rate are also retained in
  the full reference, with their distinct denominators and exclusions. They are
  not added to the page or used as coaching thresholds.

Ten-second values are rebuilt from source observations, not averages of paired
five-second means. For example, mean stroke length requires weighting by the
number of complete strokes, and speed requires the observed duration denominator.

For each phase and metric, weight experts equally, then each expert's eligible
recordings equally, then the eligible windows within that recording equally.
Short-phase samples follow the same equal-window rule, not duration weighting;
their actual duration and kind remain visible in the reference.
The exported distributions use the full expert cohort; a future participant
comparison must still exclude that participant before reweighting. Window counts
are not counts of independent experts.

## Results

| Measure | Existing 5 s | Initial strict 10 s (v1) | 10 s plus whole short phases (v2) |
| --- | ---: | ---: | ---: |
| Samples before eligibility filters | 1,216 | 576 | 578 |
| Eligible speed/removal samples | 867 | 407 | 409 |
| Eligible mean stroke length samples | 544 | 292 | 294 |
| Eligible endpoint-frequency samples | 765 | 358 | 360 |
| Eligible legacy drilling-time rate samples | 682 | 336 | 338 |

The two added samples are early-phase intervals in P7T1 (4.3571837 s) and P9T4
(9.6436315 s). Both satisfy the existing eligibility checks. The original 576
full-window measurements and all 4,710 stroke rows are unchanged. Native bounds
and source identities are retained in each reference recording. A missing metric
still remains missing; the fallback does not waive coverage or stroke eligibility.
An independent audit of all 117 source phase intervals confirmed those are the
only two positive intervals shorter than 10 s. Their native elapsed bounds are
35.842649-40.199833 s and 44.537167-54.180799 s, respectively.

Weighted medians (the 10 s column now refers to v2, including short phases):

| Phase | Speed 5 s / 10 s (mm/s) | Stroke length 5 s / 10 s (mm) | Removal 5 s / 10 s (mm3/s) |
| --- | ---: | ---: | ---: |
| Early phase | 15.727 / 16.389 | 7.864 / 7.323 | 17.529 / 17.099 |
| Antrum | 10.984 / 11.993 | 6.369 / 6.601 | 14.030 / 13.844 |
| Incus | 7.345 / 8.403 | 3.939 / 4.031 | 4.264 / 5.062 |

All three phase distributions retain five contributing experts. However, eligible
recordings differ: speed has 31/34/37 at 5 s versus 28/32/34 in v2
(early/antrum/incus); stroke length has 28/29/31 versus 25/28/29.
Whole short phases no longer disappear merely for being shorter than 10 s.
The remaining differences involve valid-data coverage, eligible stroke endpoints
and omitted tails of longer phases. Changes reflect both aggregation and retained
observations; they do not establish an optimal duration or improved event accuracy.
A strict duration-effect study would need a matched eligible-interval analysis.

## Verification and remaining questions

Numerical tests cover native versus display clocks, phase boundaries, incomplete
tails, observed-time speed with missing intervals, actual-duration removal, whole
strokes across boundaries, correct endpoint-frequency denominator and rejection
of mixed five/ten-second comparisons. Existing five-second protocol and historical
frequency metadata remain unchanged.

Additional v2 checks cover seven-second whole-phase rates/coverage, exactly-ten-
second and zero-duration cases, missing measurements, original stroke preservation
and rejection of shortened boundaries or nominal-ten-second denominators.
The new protocol is restricted to offline distribution export; event triggering
for variable-duration samples has not been activated.

The additional reference is separate from persistence. Requiring two consecutive
five-second windows is not equivalent to measuring one ten-second window.
This work did not rerun a resident detector, determine false-positive rates,
validate a clinical threshold or build expert safety/visibility distributions.
The independent old-sidecar compatibility issue is recorded in the author follow-up.

Latest v2 validation: 562 tests passed, along with Ruff for `src`, `tests` and
the export script. Nine prior-protocol synthetic artifacts remain identical;
all 42 protected files, including v1 results, retain their hashes. The raw-source
interval audit and original-full-window parity receipt are in
`.local/validation/short_phase_distribution_20261109_v2/verification.json`.

Initial v1 validation: 551 tests passed. Ruff passed for `src`, `tests` and the
added export script. Six legacy/default five-second synthetic artifacts match
the pre-task implementation exactly, including enriched metadata and digests;
all 40 protected files remain hash-identical. An initial new test used the wrong
coverage field name and was corrected. A broader repository Ruff run reported
11 pre-existing issues in four untouched helper files; no unrelated cleanup was
performed. Verification receipts are in
`.local/validation/ten_second_distribution_20261109_v1/`.
