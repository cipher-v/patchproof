# PatchProof hard-mode result

Declared run: `hard-mode-v4-gemini-3.6-flash-vertex-2026-08-28`
Model: `gemini-3.6-flash`

Provider surface: `VERTEX_AI`


## Outcomes

- Cases: 5 (4 historical, 1 synthetic)
- Claims selected: 5/5 (100.0%)
- Discriminating generated tests: 2/5 (40.0%)
- Claim-supported scenarios: 2/5 (40.0%)
- Candidate attempts: 8 (3 repairs)
- Validated candidates: 8
- Invalid candidate structured outputs: 0
- Environmental candidate evaluations: 6
- Incorrect supports versus oracle direction: 0

## Terminal statuses

- `CLAIM_SUPPORTED_FOR_SCENARIO`: 2
- `ENVIRONMENTAL`: 3

## Model accounting

- Completed logical model results: 15
- Provider attempts represented by completed results: 15
- Failed logical model calls: 0
- Failed provider attempts: unavailable from the adapter after terminal invocation failure
- Prompt tokens where reported: 51577
- Output tokens where reported: 3350
- Total tokens where reported: 57446
- Total model duration: 86.323 seconds

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
