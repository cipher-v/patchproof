# PatchProof hard-mode result

Declared run: `hard-mode-v5-gemini-3.6-flash-vertex-2026-08-29`
Model: `gemini-3.6-flash`

Provider surface: `VERTEX_AI`


## Outcomes

- Cases: 5 (4 historical, 1 synthetic)
- Claims selected: 5/5 (100.0%)
- Discriminating generated tests: 3/5 (60.0%)
- Claim-supported scenarios: 3/5 (60.0%)
- Candidate attempts: 7 (2 repairs)
- Validated candidates: 6
- Invalid candidate structured outputs: 0
- Environmental candidate evaluations: 1
- Incorrect supports versus oracle direction: 0

## Terminal statuses

- `CLAIM_SUPPORTED_FOR_SCENARIO`: 3
- `INVALID_TEST`: 1
- `NON_DISCRIMINATING`: 1

## Model accounting

- Completed logical model results: 15
- Provider attempts represented by completed results: 15
- Failed logical model calls: 0
- Failed provider attempts: unavailable from the adapter after terminal invocation failure
- Prompt tokens where reported: 49864
- Output tokens where reported: 3287
- Total tokens where reported: 53292
- Total model duration: 96.702 seconds

## Model-call budget preflight

- Maximum possible logical model calls: 20
- Maximum provider attempts per logical call: derived as 1 plus permitted transient retries
- Maximum possible provider calls: 40
- Operator-declared available provider calls: 40
- Preflight passed: True

A logical model call is one semantic PatchProof task. A provider attempt is an actual provider
request, including any permitted transient retry. The available capacity is an operator declaration,
not a query of the provider's live remaining quota.


## Scope

This is a five-case adversarial diagnostic with fixed denominators, not a representative production accuracy estimate or proof of pull-request correctness.
