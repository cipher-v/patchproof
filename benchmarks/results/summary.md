# PatchProof Bench Summary

Methodology: `REFERENCE_ORACLE_POLICY_REPLAY`.

- Historical PR cases: 4 across 2 repositories
- Executed scenarios: 8
- Developer oracles reproduced as BASE assertion failure / HEAD pass: 4/4
- Controlled weak candidates rejected as non-discriminating: 4/4
- Controlled failure/recovery checks passed: 18/18 (100.0%) across 9 selected nodes
- Mean two-revision challenge latency: 1.982 seconds

## Policy comparison

| Strategy | Scenarios | Supports | True supports | False supports | False/support | False/negative |
|---|---:|---:|---:|---:|---:|---:|
| HEAD_ONLY | 8 | 8 | 4 | 4 | 50.0% | 100.0% |
| PATCHPROOF_BASE_HEAD | 8 | 4 | 4 | 0 | 0.0% | 0.0% |

`HEAD_ONLY` treats a passing candidate on HEAD as support. `PATCHPROOF_BASE_HEAD` requires the identical artifact to fail by assertion on BASE and pass on HEAD. The controlled negative uses an intentionally weak candidate and an unchanged buggy revision.

## Not measured

- existing upstream test-suite baseline
- live Gemini candidate generation
- end-to-end semantic claim accuracy
- model calls and token usage

These are reference-oracle and policy-mechanics results, not a live Gemini generation quality claim or a representative production false-support estimate.
