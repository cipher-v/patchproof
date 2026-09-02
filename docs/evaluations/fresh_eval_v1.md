# PatchProof sealed fresh evaluation v1

## Purpose and scope

`fresh-eval-v1` is a one-shot evaluation of PatchProof's frozen Phase-2 claim
investigation and evidence-generation workflow. It measures whether the system can
support independently verified behavioral changes while abstaining on controls that do
not represent product behavioral changes.

Here, **fresh** means that the cases were not part of PatchProof's development regression
corpus before the implementation was frozen. No assertion is made that Gemini had never
encountered them or that they were absent from model training data. All cases are public
pull requests.

On the sealed `fresh-eval-v1` benchmark, PatchProof supported 8/8 positive
behavioral-change cases and correctly abstained on 7/8 negative controls, with no
environment, provider, or harness failures.

## Sealing methodology

The product implementation was frozen at commit
`851b8342b3aac6a0c1664c519b5c1827a1fe6079` before this evaluation set was used for
inference. The 16-case dataset was then sealed separately at commit `2286cfc`. Exact
repository BASE and HEAD revisions, exclusions, hidden positive oracles, and integrity
hashes were fixed before inference.

Preparation produced a label-blind public case document and deterministic context hash
for every case. Labels, expected interpretations, construction-readiness labels, and
hidden-oracle paths, hashes, function names, and source were unavailable to inference.
Changed upstream Python tests remained excluded.

The inference harness used the unchanged Phase-2 product path, a fixed 60-second delay
between cases, and an exactly-once journal. It recorded `RUN_STARTED` before the first
model invocation and persisted every case result immediately. The run used Vertex AI and
`gemini-3.6-flash`. Scoring revealed the sealed labels only after all 16 raw results had
been persisted.

## Dataset composition

| Group | Cases | Intended measurement |
| --- | ---: | --- |
| Positive behavioral changes | 8 | Generate mechanically discriminating, semantically related evidence for a real behavioral change. |
| Negative controls | 8 | Abstain when a PR does not establish an applicable product behavioral claim. |
| Total | 16 | Balanced, repository-diverse sealed evaluation. |

Each positive case had an independent hidden oracle proving
`BASE=ASSERTION_FAILED` and `HEAD=PASSED` before inference. Negative-control labels were
not available to the claim investigator or evidence workflow.

## Headline results

| Metric | Result |
| --- | ---: |
| Correct benchmark classifications | 15/16 (93.75%) |
| Positive support rate / recall | 8/8 (100%) |
| Negative-control abstention rate | 7/8 (87.5%) |
| Support precision | 8/9 (approximately 88.89%) |
| Support F1 | 16/17 (approximately 94.12%) |
| Missed positives | 0 |
| Incorrect supports | 1 |

These figures describe only this sealed 16-case benchmark. They do not establish
universal accuracy, broad generalization, or production readiness.

## Positive-side results

| Case | Classification | Candidate attempts |
| --- | --- | ---: |
| `tomlkit-551` | `SUPPORTED_POSITIVE` | 1 |
| `werkzeug-3255` | `SUPPORTED_POSITIVE` | 1 |
| `urllib3-5071` | `SUPPORTED_POSITIVE` | 1 |
| `tablib-651` | `SUPPORTED_POSITIVE` | 2 |
| `boltons-418` | `SUPPORTED_POSITIVE` | 1 |
| `typeguard-566` | `SUPPORTED_POSITIVE` | 2 |
| `jsonlogger-66` | `SUPPORTED_POSITIVE` | 1 |
| `boltons-410` | `SUPPORTED_POSITIVE` | 1 |

All eight positive cases ended with mechanically discriminating evidence and a semantic
`RELATED` assessment. There were no missed positives.

## Negative-control results

| Case | Classification | Terminal result |
| --- | --- | --- |
| `requests-7431` | `EXPECTED_ABSTENTION` | `CLAIM_COUNTERFACTUAL_NOT_APPLICABLE` |
| `typeguard-555` | `EXPECTED_ABSTENTION` | `CLAIM_INSUFFICIENT_EVIDENCE` |
| `attrs-1496` | `EXPECTED_ABSTENTION` | `CLAIM_COUNTERFACTUAL_NOT_APPLICABLE` |
| `tablib-443` | `EXPECTED_ABSTENTION` | `CLAIM_INSUFFICIENT_EVIDENCE` |
| `h11-183` | `EXPECTED_ABSTENTION` | `CLAIM_COUNTERFACTUAL_NOT_APPLICABLE` |
| `click-3499` | `EXPECTED_ABSTENTION` | `CLAIM_INSUFFICIENT_EVIDENCE` |
| `itsdangerous-322` | `EXPECTED_ABSTENTION` | `CLAIM_COUNTERFACTUAL_NOT_APPLICABLE` |
| `requests-6951` | `INCORRECT_SUPPORT` | `CLAIM_SUPPORTED_FOR_SCENARIO` |

Seven controls stopped before candidate generation. `requests-6951` was the only false
positive and the only negative control for which a claim and candidate were generated.

## Reliability and runtime

| Reliability measure | Count |
| --- | ---: |
| Environment failures | 0 |
| Provider/model invocation failures | 0 |
| Harness/implementation failures | 0 |
| Invalid claim outputs | 0 |
| Potential regressions | 0 |

The run started at `2026-09-02T08:39:30.736462+00:00` and completed at
`2026-09-02T09:24:47.127611+00:00`. Total measured wall time was
`2716.3917518998496` seconds, approximately 45 minutes 16 seconds.

The artifacts contain 36 logical model-result records: 16 claim investigations, 11
candidate generations, and 9 semantic assessments. All recorded calls used one provider
attempt. Recorded accounting totals were 131,690 prompt tokens, 6,344 output tokens, and
167,820 total tokens. Reasoning-token values were present for 27 of the 36 records and
sum to 29,786; the remaining nine values were not reported and are not imputed.

## Repair-loop behavior

| Measure | Result |
| --- | ---: |
| Claims selected | 9 |
| Claim abstentions | 7 |
| Candidate attempts | 11 |
| Cases requiring repair | 2 |
| Maximum attempts used by a case | 2 |
| Mechanically discriminating cases | 9 |
| Semantic `RELATED` supports | 9 |

`typeguard-566` demonstrates the evidence gate and bounded repair loop. Its initial
candidate produced `BASE=TEST_ERROR` and `HEAD=PASSED`. PatchProof correctly refused to
treat this as support because an escaping exception is not admissible assertion evidence.
The evidence-driven repair converted both outcomes into an explicitly asserted
observation. The repaired candidate then produced `BASE=ASSERTION_FAILED` and
`HEAD=PASSED`; semantic assessment returned `RELATED`, yielding
`CLAIM_SUPPORTED_FOR_SCENARIO`.

`tablib-651` was the other case that required a second candidate attempt. No case used
more than one repair, although the product's fixed policy allowed up to two.

## False-positive analysis: `requests-6951`

The sealed label for `requests-6951` is negative control. The PR changed documentation
content, but the changed Sphinx configuration was executable Python. Phase 2 selected
`docs/conf.py::copyright` as a shared observable and proposed the claim that importing
`docs/conf.py` would expose the HEAD copyright string without an HTML link.

PatchProof generated a valid test using `runpy.run_path("docs/conf.py")`. The identical
candidate bytes produced:

```text
BASE = ASSERTION_FAILED
HEAD = PASSED
mechanical pattern = BASE_ASSERTION_FAILED_HEAD_PASSED
semantic relation = RELATED
```

Given the selected claim, the candidate and mechanical evidence were valid, and the
semantic assessor correctly found that the assertion tested that claim. This was not an
execution, environment, provider, or evidence-classification failure.

It nevertheless remains an **incorrect support** under the sealed benchmark label. The
benchmark asks whether a PR supports a product behavioral claim, and this documentation
configuration value was not intended to qualify as product behavior. The false positive
therefore identifies an overly broad claim-applicability boundary: executable
documentation/configuration state was eligible for behavioral evidence merely because it
was shared, importable, and mechanically testable. The benchmark label is retained, and
no post-evaluation relabeling or evidence reinterpretation is applied.

## Limitations

- The benchmark contains only 16 cases and is too small to characterize performance over
  the full population of Python pull requests.
- The cases are public pull requests and may have appeared in model training data.
- "Fresh" means absent from PatchProof's development regression corpus before the product
  implementation was frozen; it makes no claim about prior model exposure.
- The results apply to the frozen implementation, prompt set, evidence policy,
  `gemini-3.6-flash`, Vertex AI, and the evaluated execution environment.
- The balanced positive/control composition is deliberate and does not represent the
  natural prevalence of behavioral changes in arbitrary pull requests.
- One false positive shows that mechanically valid evidence does not by itself guarantee
  that the selected observable belongs within the intended product-behavior boundary.
- This evaluation does not establish universal behavior or production readiness.

## Reproducibility and audit trail

The permanent record references, but does not copy or alter, the local sealed runtime
artifacts. The raw results, scored results, and append-only journal remain under
`.patchproof/fresh-eval-v1-sealed/`. The prepared public document binds every case to its
deterministic context, and the journal preserves the exactly-once execution sequence.

### Artifact hashes

| Artifact | SHA-256 |
| --- | --- |
| `live/raw_results.json` | `93dfd522ddd490184882f5914459c71f02c9318d15454760cdb66a9449af5bda` |
| `live/scored_results.json` | `d346c2faa8419fffb692c1a360c40bc8e9f7ef2b32eed3db1b2a126d64edbbf6` |
| `live/journal.jsonl` | `cd6dc68babcbf937738115970fa67014bfb71eca3148dd424fcf8bd5917b465d` |
| `public_cases.json` | `e2eb3062430a542221a0380cea755b8d5b548505c72f3aed24573e6af5f2ff21` |
| Sealed `manifest.json` | `051e1d72e1f76ccd9c2565bee871b50f1326b2a555ba985134e6b7f26df05e74` |

### Frozen commits and tags

| Item | Commit/tag |
| --- | --- |
| Frozen product implementation | `851b8342b3aac6a0c1664c519b5c1827a1fe6079` |
| Sealed dataset commit | `2286cfc` |
| Final inference harness commit | `5e651c344cdc369b9b00cff335a49f58e2fcd215` |
| Frozen product tag | `phase2-dev-10of10` |
| Dataset tag | `fresh-eval-v1-sealed` |
| Final runner tag | `fresh-eval-v1-runner-final` |

## No-rerun policy

`fresh-eval-v1` is a completed one-shot sealed evaluation. It will **not** be rerun after
product changes. Future changes, including any narrowing prompted by `requests-6951`,
must be evaluated on a newly declared and independently sealed dataset rather than by
replacing or improving this result.
