# MotionLedger evaluation results

This file is a scaffold. No metrics are reported until labelled footage and predictions are available.

Evaluation clips specified: **12**

| Clip | Scenario | Expected finding labels |
|---|---|---|
| 01-correct | Correct run | None |
| 02-no-tightening | No bolt tightened | OMISSION:S3 |
| 03-hole-3-skipped | Bolt 3 not tightened | INCOMPLETE:S3@hole_3 |
| 04-order | Two bolts inserted before the gasket | ORDER_VIOLATION:S2 |
| 05-pattern | Tightened 1-2-3-4 | PATTERN_VIOLATION:S3 |
| 06-three-bolts | Only three bolts inserted | INCOMPLETE:S2, POSTCONDITION_FAILED:P1 |
| 07-short-touch | 0.3 second wrench touch on bolt 2 | INCOMPLETE:S3@hole_2 |
| 08-occlusion | Arm hides hole 4 during tightening | UNVERIFIABLE:S3@hole_4 |
| 09-repeated-gasket | Gasket placed, lifted and placed again | REPEATED:S1 |
| 10-extra-hammer | Extra hammer taps | EXTRA_ACTION |
| 11-lighting-angle | Correct run, different lighting and camera angle | None |
| 12-faster-run | Correct run, second operator, faster pace | None |

## Required metrics

- Per-type precision, recall and F1 at temporal IoU ≥ 0.3.
- False-accusation rate; target <5%.
- Unverifiable rate on clean clips; target <10%.
- Median observed-step timestamp error; target <0.5 s.
- Reviewer time saved versus raw-video scrubbing.
